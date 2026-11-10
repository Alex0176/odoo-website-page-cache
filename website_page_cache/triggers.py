# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Change journal fed by database triggers.

Every statement that inserts, updates or deletes rows in a table a public page is built from
leaves rows ``(model, res_id, xid)`` in ``website_page_cache_log``. Because this happens in the
database, it catches everything: ORM writes, translations (stored in the same jsonb columns),
module upgrades, imports and plain SQL.

Tags: ``<model>:<id>`` for a record, ``<model>:0`` for "the set of records of that model changed"
(created, deleted, re-ordered, re-categorised) and ``*:0`` for things that can change every page
(views, languages, menus, taxes, currencies, asset bundles, ...).

The trigger functions can never make a write fail: they swallow their own errors.
"""
import logging
import re

_logger = logging.getLogger(__name__)

LOG_TABLE = "website_page_cache_log"
PREFIX = "wpc_log_"
TRIGGER_NAME = "website_page_cache"

IMAGE_MODELS = (
    "product.template", "product.product", "product.image", "product.public.category", "blog.post",
    "blog.blog", "website", "res.company", "res.partner",
)


def row(model, expr, where=None, ignore=()):
    return {"kind": "row", "model": model, "expr": expr, "where": where, "ignore": ignore}


def struct(model, where=None, ignore=()):
    return {"kind": "struct", "model": model, "expr": "0", "where": where, "ignore": ignore}


def glob(where=None):
    return {"kind": "global", "model": "*", "expr": "0", "where": where, "ignore": ()}


def template_rel(column, struct_too=True):
    items = [row("product.template", f"r.{column}")]
    if struct_too:
        items.append(struct("product.template"))
    return items


TEMPLATE_OF_PRODUCT = "(SELECT p.product_tmpl_id FROM product_product p WHERE p.id = {})"

SPECS = {
    "product_template": [row("product.template", "r.id"), struct("product.template")],
    "product_product": [row("product.template", "r.product_tmpl_id"), row("product.product", "r.id")],
    "stock_quant": [row("product.template", TEMPLATE_OF_PRODUCT.format("r.product_id"))],
    "product_template_attribute_line": template_rel("product_tmpl_id", struct_too=False),
    "product_template_attribute_value": template_rel("product_tmpl_id", struct_too=False),
    "product_image": template_rel("product_tmpl_id", struct_too=False),
    "product_public_category_product_template_rel": template_rel("product_template_id"),
    "product_alternative_rel": template_rel("src_id", struct_too=False),
    "product_accessory_rel": template_rel("src_id", struct_too=False),
    "product_combo_product_template_rel": template_rel("product_template_id", struct_too=False),
    "product_style_product_template_rel": template_rel("product_template_id", struct_too=False),
    "product_faqs_product_template_rel": template_rel("product_template_id", struct_too=False),
    "product_tag_product_product_rel": [
        row("product.template", TEMPLATE_OF_PRODUCT.format("r.product_product_id")), struct("product.template")],
    "blog_post": [row("blog.post", "r.id", ignore=("visits",)), struct("blog.post", ignore=("visits",))],
    "blog_post_blog_tag_rel": [row("blog.post", "r.blog_post_id"), struct("blog.post")],
    "res_partner": [row("res.partner", "r.id")],
    "ir_attachment": [
        row("*", "0", where="r.url LIKE '/web/assets/%'"),
        row("ir.attachment", "r.id", where="r.res_model IS NULL OR r.res_model = 'ir.ui.view'"),
        row("r.res_model", "r.res_id", where="r.res_id > 0 AND r.res_model IN (%s)" % ", ".join(
            "'%s'" % m for m in IMAGE_MODELS)),
        row("product.template", TEMPLATE_OF_PRODUCT.format("r.res_id"),
            where="r.res_model = 'product.product' AND r.res_id > 0"),
    ],
    "ir_config_parameter": [glob(
        "(r.key LIKE 'website%' OR r.key LIKE 'sale.%' OR r.key LIKE 'product.%' OR r.key LIKE 'web.base.url%') "
        "AND r.key NOT LIKE 'website_page_cache.%'")],
}
for _table in (
    "ir_ui_view", "ir_asset", "website", "website_page", "website_menu", "website_rewrite", "website_lang_rel",
    "res_lang", "res_company", "res_currency", "res_currency_rate", "account_tax", "account_fiscal_position",
    "account_fiscal_position_tax", "product_pricelist", "product_pricelist_item", "product_public_category",
    "product_category", "product_attribute", "product_attribute_value", "product_tag", "product_ribbon",
    "blog_blog", "blog_tag", "website_snippet_filter",
):
    SPECS[_table] = [glob()]


def _selects(items, op):
    """UNION ALL of one SELECT per emitter for the operation ('i'nsert, 'd'elete, 'u'pdate)."""
    parts = []
    sources = {"i": ("new_rows",), "d": ("old_rows",), "u": ("old_rows", "new_rows")}[op]
    for item in items:
        model = item["model"]
        model_sql = model if model.startswith("r.") else f"'{model}'"
        limit = " LIMIT 1" if item["kind"] != "row" else ""
        where = f" WHERE {item['where']}" if item["where"] else ""
        if op == "u" and item["ignore"]:
            # only a change in a column other than the ignored ones counts (e.g. the visit counter)
            ign = "ARRAY[%s]" % ", ".join("'%s'" % c for c in item["ignore"])
            extra = f" AND ({item['where']})" if item["where"] else ""
            parts.append(
                f"(SELECT {model_sql}::varchar, {item['expr']} FROM old_rows o JOIN new_rows r ON r.id = o.id "
                f"WHERE to_jsonb(r) - {ign} IS DISTINCT FROM to_jsonb(o) - {ign}{extra}{limit})")
            continue
        for src in sources:
            parts.append(f"(SELECT {model_sql}::varchar, {item['expr']} FROM {src} r{where}{limit})")
    return " UNION ALL ".join(parts)


def function_sql(table, items):
    insert = (
        f"INSERT INTO {LOG_TABLE} (model, res_id, xid) "
        "SELECT DISTINCT model, res_id, pg_current_xact_id()::text::bigint FROM ({}) s(model, res_id) WHERE res_id IS NOT NULL"
    )
    return f"""
CREATE OR REPLACE FUNCTION {PREFIX}{table}() RETURNS trigger LANGUAGE plpgsql AS $wpc$
BEGIN
    BEGIN
        IF TG_OP = 'INSERT' THEN
            {insert.format(_selects(items, "i"))};
        ELSIF TG_OP = 'DELETE' THEN
            {insert.format(_selects(items, "d"))};
        ELSE
            {insert.format(_selects(items, "u"))};
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'website_page_cache: journal not written for {table}: %', SQLERRM;
    END;
    RETURN NULL;
END
$wpc$
"""


def _columns_used(items):
    text = " ".join(f"{i['expr']} {i['where'] or ''} {i['model']}" for i in items)
    cols = set(re.findall(r"\br\.([a-z_0-9]+)", text))
    cols.update(c for i in items for c in i["ignore"])
    if any(i["ignore"] for i in items):
        cols.add("id")
    return cols


def _schema_tables(cr):
    cr.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {r[0] for r in cr.fetchall()}


def _columns(cr, table):
    cr.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s", [table])
    return {r[0] for r in cr.fetchall()}


def _installed_triggers(cr):
    cr.execute(
        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname LIKE %s AND NOT t.tgisinternal", [TRIGGER_NAME + "_%"])
    return {r[0] for r in cr.fetchall()}


def ensure_log_table(cr):
    cr.execute(f"""
        CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
            id bigserial PRIMARY KEY,
            model varchar NOT NULL,
            res_id integer NOT NULL DEFAULT 0,
            xid bigint NOT NULL,
            create_date timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
        );
        CREATE INDEX IF NOT EXISTS {LOG_TABLE}_xid_idx ON {LOG_TABLE} (xid);
        CREATE INDEX IF NOT EXISTS {LOG_TABLE}_create_date_idx ON {LOG_TABLE} (create_date);
    """)


def missing_tables(cr):
    """Tables that exist but have no trigger yet."""
    have = _schema_tables(cr)
    done = _installed_triggers(cr)
    return [t for t in SPECS if t in have and t not in done]


def ensure_triggers(cr, force=False):
    """Create the journal table and every missing trigger; returns the tables that got one."""
    ensure_log_table(cr)
    have = _schema_tables(cr)
    done = set() if force else _installed_triggers(cr)
    created = []
    for table, items in SPECS.items():
        if table not in have or table in done:
            continue
        if not _columns_used(items) <= _columns(cr, table):
            _logger.warning("page cache: table %s lacks columns the journal needs, skipped", table)
            continue
        try:
            cr.execute("SAVEPOINT wpc_trigger")
            cr.execute(function_sql(table, items))
            for op, event, ref in (
                ("ins", "INSERT", "NEW TABLE AS new_rows"),
                ("upd", "UPDATE", "OLD TABLE AS old_rows NEW TABLE AS new_rows"),
                ("del", "DELETE", "OLD TABLE AS old_rows"),
            ):
                name = f"{TRIGGER_NAME}_{op}"
                cr.execute(f'DROP TRIGGER IF EXISTS {name} ON "{table}"')
                cr.execute(
                    f'CREATE TRIGGER {name} AFTER {event} ON "{table}" REFERENCING {ref} '
                    f"FOR EACH STATEMENT EXECUTE FUNCTION {PREFIX}{table}()")
            cr.execute("RELEASE SAVEPOINT wpc_trigger")
            created.append(table)
        except Exception:
            cr.execute("ROLLBACK TO SAVEPOINT wpc_trigger")
            _logger.exception("page cache: cannot create trigger on %s", table)
    return created


def drop_triggers(cr):
    cr.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname LIKE %s AND NOT t.tgisinternal", [TRIGGER_NAME + "_%"])
    for table, name in cr.fetchall():
        cr.execute(f'DROP TRIGGER IF EXISTS "{name}" ON "{table}"')
    cr.execute(
        "SELECT proname FROM pg_proc WHERE proname LIKE %s AND pronamespace = current_schema()::regnamespace",
        [PREFIX + "%"])
    for (name,) in cr.fetchall():
        cr.execute(f'DROP FUNCTION IF EXISTS "{name}"()')


def drop_all(cr):
    drop_triggers(cr)
    cr.execute(f"DROP TABLE IF EXISTS {LOG_TABLE}")
