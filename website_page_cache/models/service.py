# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import time

from odoo import api, models

from .. import changes, fit, settings, stats, store, triggers

_logger = logging.getLogger(__name__)

ADVISORY_LOCK = 7355608
JOURNAL_MIN_DAYS = 2
STATS_KEEP_DAYS = 90


class PageCacheService(models.AbstractModel):
    _name = "website.page.cache.service"
    _description = "Website page cache maintenance"

    def _register_hook(self):
        super()._register_hook()
        try:
            self._ensure_triggers()
        except Exception:
            _logger.exception("page cache: cannot check the change triggers")

    @api.model
    def _ensure_triggers(self, force=False):
        """Create the change journal and its triggers where missing. Cached pages from before a
        trigger existed cannot be trusted, so the cache is emptied when one is added."""
        cr = self.env.cr
        if not force and not triggers.missing_tables(cr) and self._journal_exists():
            return []
        cr.execute("SELECT pg_advisory_xact_lock(%s)", [ADVISORY_LOCK])  # one process creates, the rest wait
        created = triggers.ensure_triggers(cr, force=force)
        if created:
            _logger.info("page cache: change triggers created on %d tables", len(created))
            store.clear_all(cr.dbname)
            changes.forget(cr.dbname)
            settings.reset(cr.dbname)
        return created

    @api.model
    def _journal_exists(self):
        self.env.cr.execute("SELECT to_regclass(%s)", [triggers.LOG_TABLE])
        return self.env.cr.fetchone()[0] is not None

    @api.model
    def _params(self):
        return settings.read_params(self.env.cr)

    @api.model
    def _decide_backend(self):
        """For ``backend = auto``: RAM while the sites fit into it, otherwise files. Once on RAM the
        choice is kept while the verdict is "fits" or "tight" so it does not flap."""
        params = self._params()
        if params["backend"] != "auto":
            return
        budget = int(float(params["max_mb_ram"] or 0) * 1048576)
        verdict = fit.estimate(self.env, self.env.cr.dbname, budget)["verdict"]
        keep_ram = params["auto_backend"] == "ram" and verdict in ("fits", "tight")
        new = "ram" if verdict == "fits" or keep_ram else "file"
        if new != params["auto_backend"]:
            _logger.info("page cache: automatic backend %s -> %s (%s)", params["auto_backend"], new, verdict)
            self.env["ir.config_parameter"].sudo().set_param(settings.PREFIX + "auto_backend", new)
            settings.reset(self.env.cr.dbname)

    @api.model
    def _sweep(self):
        """Delete entries that are expired or stale, and everything held by an inactive backend."""
        cr = self.env.cr
        dbname = cr.dbname
        st = settings.get(cr, dbname)
        removed = {"inactive": 0, "stale": 0, "expired": 0, "trimmed": 0}
        for backend in store.BACKENDS:
            if backend != st["backend"]:
                removed["inactive"] += store.usage(backend, dbname)[0]
                store.clear(backend, dbname)
        if not st["ready"]:
            return removed
        index = changes.refresh(cr, dbname, st["ttl"] + 3600)
        now = time.time()
        per_website = {}
        for path, _size, _mtime in list(store.entries(st["backend"], dbname)):
            meta = store.read_meta(path)
            if not meta:
                store.drop(path)
                continue
            if now - meta.get("created", 0) > st["ttl"]:
                reason = "expired"
            elif index.stale(meta.get("deps", ()), changes.parse_snapshot(meta["snapshot"])):
                reason = "stale"
            else:
                continue
            if store.drop(path):
                removed[reason] += 1
                site = meta.get("website_id")
                if site:
                    per_website[site] = per_website.get(site, 0) + 1
        for site, number in per_website.items():
            stats.count(dbname, site, stale=number)
        removed["trimmed"] = store.trim(st["backend"], dbname, st["max_bytes"])
        return removed

    @api.model
    def _prune(self):
        cr = self.env.cr
        ttl_days = settings.get(cr, cr.dbname)["ttl"] / 86400
        days = max(JOURNAL_MIN_DAYS, ttl_days * 2)
        cr.execute(
            f"DELETE FROM {triggers.LOG_TABLE} WHERE create_date < (now() AT TIME ZONE 'utc') - %s * interval '1 day'",
            [days])
        cr.execute(
            "DELETE FROM website_page_cache_stat WHERE date < (now() AT TIME ZONE 'utc')::date - %s", [STATS_KEEP_DAYS])

    @api.model
    def _cron_maintain(self):
        self._ensure_triggers()
        self._decide_backend()
        removed = self._sweep()
        self._prune()
        stats.flush(self.env.cr.dbname)
        _logger.info("page cache maintenance: %s", removed)

    @api.model
    def _clear(self):
        store.clear_all(self.env.cr.dbname)
        changes.forget(self.env.cr.dbname)
        settings.reset(self.env.cr.dbname)
