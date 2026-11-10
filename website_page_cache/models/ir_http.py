# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging
import random
import re
import time

from odoo import models
from odoo.http import Response, request
from odoo.tools import sql

from .. import changes, settings, stats, store

_logger = logging.getLogger(__name__)

# Public, parameterless pages whose content depends only on database content, language,
# pricelist and fiscal position. The language prefix is optional.
LANG = r"(?:/[a-z]{2,3}(?:_[A-Za-z0-9]{2,4})?)?"
SHOP_LISTING = re.compile(rf"^{LANG}/shop(?:/page/\d+|/category/[^/]+-\d+(?:/page/\d+)?)?$")
BLOG_LISTING = re.compile(rf"^{LANG}/blog(?:/[^/]+(?:/tag/[^/]+)?(?:/page/\d+)?)?$")
CACHEABLE_PATHS = re.compile(
    rf"^{LANG}(?:"
    r"/shop(?:/page/\d+)?"
    r"|/shop/category/[^/]+-\d+(?:/page/\d+)?"
    r"|/shop/[^/]+-\d+"
    r"|/blog"
    r"|/blog/[^/]+(?:/tag/[^/]+)?(?:/page/\d+)?"
    r"|/blog/[^/]+/[^/]+-\d+"
    r")$"
)
BLOG_POST_PATH = re.compile(rf"^{LANG}/blog/[^/]+/[^/]+-(\d+)$")
# A session of an anonymous visitor may contain only these keys; anything else (cart, login of a
# portal user, checkout state, ...) means the page may differ from the shared one.
ALLOWED_SESSION_KEYS = {
    "_trace", "context", "create_time", "db", "debug", "fiscal_position_id", "login", "posts_viewed",
    "session_token", "uid", "website_sale_current_pl",
}
CSRF_TOKEN = re.compile(rb"(?<![0-9a-f])[0-9a-f]{40}o\d{9,12}")
CSRF_PLACEHOLDER = b"@@website_page_cache:csrf@@"
KEPT_HEADERS = ("Content-Type", "Cache-Control", "Link", "X-Robots-Tag", "Content-Language")

# records a page shows, found in its HTML: product links, blog links and /web/image URLs
LINK_PRODUCT = re.compile(rb"/shop/(?!category/)[^/\"'?#\s]*?-(\d+)(?![\w-])")
LINK_PRODUCT_ATTR = re.compile(rb"data-product-template-id=[\"'](\d+)[\"']")
LINK_BLOG_POST = re.compile(rb"/blog/[^/\"'?#\s]+/[^/\"'?#\s]*?-(\d+)(?![\w-])")
LINK_IMAGE = re.compile(rb"/web/image/([a-z][a-z0-9_.]*)/(\d+)/")
MAX_DEPS = 3000


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _dispatch(cls, endpoint):
        started = time.monotonic()
        ctx = None
        try:
            ctx = cls._page_cache_context(endpoint)
        except Exception:
            _logger.exception("page cache: lookup failed, rendering normally")
        if ctx:
            try:
                hit = cls._page_cache_hit(ctx)
            except Exception:
                _logger.exception("page cache: reading failed, rendering normally")
                hit = None
            if hit is not None:
                stats.count(request.db, ctx["website_id"], hits=1, hit_time=time.monotonic() - started,
                            bytes_served=len(hit.get_data()))
                cls._page_cache_flush_stats()
                return hit
            try:
                ctx["snapshot"] = changes.current_snapshot(request.env.cr)
            except Exception:
                _logger.exception("page cache: no snapshot, rendering without storing")
                ctx = None
        rendered = time.monotonic()
        result = super()._dispatch(endpoint)
        if ctx:
            try:
                result = cls._page_cache_store(ctx, result, time.monotonic() - rendered)
            except Exception:
                _logger.exception("page cache: storing failed")
            cls._page_cache_flush_stats()
        return result

    @classmethod
    def _page_cache_flush_stats(cls):
        try:
            stats.maybe_flush(request.db)
        except Exception:
            _logger.exception("page cache: statistics flush failed")

    @classmethod
    def _page_cache_context(cls, endpoint):
        httprequest = request.httprequest
        if (
            httprequest.method != "GET"
            or httprequest.query_string
            or httprequest.headers.get("Range")
            or not endpoint.routing.get("website")
            or endpoint.routing.get("type") != "http"
            or not CACHEABLE_PATHS.match(httprequest.path)
            or not getattr(request, "website", None)
            or not request.db
        ):
            return None
        session = request.session
        if session.debug or not set(session) <= ALLOWED_SESSION_KEYS:
            return None
        if not request.env.user._is_public():
            return None
        cr = request.env.cr
        st = settings.get(cr, request.db)
        if not st["enabled"] or not st["ready"]:
            return None
        website = request.website
        pricelist = getattr(request, "pricelist", None)
        fiscal_position = getattr(request, "fiscal_position", None)
        key = [
            website.id, httprequest.host, request.lang.code, httprequest.path,
            pricelist.id if pricelist else 0,
            fiscal_position.id if fiscal_position else 0,
            bool(website._allConsentsGranted()),
        ]
        index = changes.refresh(cr, request.db, st["ttl"] + 3600)
        return {"key": key, "backend": st["backend"], "ttl": st["ttl"], "max_bytes": st["max_bytes"],
                "index": index, "website_id": website.id}

    @classmethod
    def _page_cache_hit(cls, ctx):
        found = store.load(ctx["backend"], request.db, ctx["key"])
        if not found:
            return None
        meta, body, path = found
        if time.time() - meta.get("created", 0) > ctx["ttl"] or ctx["index"].stale(
            meta.get("deps", ()), changes.parse_snapshot(meta["snapshot"])
        ):
            store.drop(path)
            stats.count(request.db, ctx["website_id"], stale=1)
            return None
        if CSRF_PLACEHOLDER in body:
            body = body.replace(CSRF_PLACEHOLDER, request.csrf_token().encode())
        response = Response(
            body, status=200, headers=[tuple(h) for h in meta["headers"]],
            qcontext={"response_template": meta.get("template")},
        )
        response.headers["X-Page-Cache"] = "HIT"
        post_id = meta.get("blog_post_id")
        if post_id:
            cls._page_cache_count_blog_visit(post_id)
        if cls.is_a_bot():
            request.session.can_save = False
        return response

    @classmethod
    def _page_cache_count_blog_visit(cls, post_id):
        # the controller counts one visit per session; a cached page must not hide it
        if post_id in request.session.get("posts_viewed", []):
            return
        post = request.env["blog.post"].sudo().browse(post_id)
        if sql.increment_fields_skiplock(post, "visits"):
            viewed = request.session.get("posts_viewed") or []
            viewed.append(post_id)
            request.session["posts_viewed"] = viewed
            request.session.touch()

    @classmethod
    def _page_cache_dependencies(cls, path, body, main_object):
        """Tags of everything the page shows (see ``triggers``)."""
        deps = {changes.GLOBAL_TAG}
        partner = request.website.company_id.partner_id
        if partner:
            deps.add(f"res.partner:{partner.id}")
        for match in LINK_PRODUCT.finditer(body):
            deps.add(f"product.template:{int(match.group(1))}")
        for match in LINK_PRODUCT_ATTR.finditer(body):
            deps.add(f"product.template:{int(match.group(1))}")
        for match in LINK_BLOG_POST.finditer(body):
            deps.add(f"blog.post:{int(match.group(1))}")
        for match in LINK_IMAGE.finditer(body):
            deps.add(f"{match.group(1).decode()}:{int(match.group(2))}")
        model = getattr(main_object, "_name", None)
        if model in ("product.template", "blog.post") and main_object.id:
            deps.add(f"{model}:{main_object.id}")
        if SHOP_LISTING.match(path):
            deps.add("product.template:0")
        if BLOG_LISTING.match(path):
            deps.add("blog.post:0")
        return sorted(deps)[:MAX_DEPS]

    @classmethod
    def _page_cache_store(cls, ctx, response, render_time):
        if (
            not isinstance(response, Response)
            or response.status_code != 200
            or response.mimetype != "text/html"
            or response.headers.get("Set-Cookie")
        ):
            return response
        body = response.get_data()
        sid = request.session.sid[:30].encode()
        if sid and sid in body:
            _logger.warning("page cache: session id in page %s, not cached", request.httprequest.path)
            return response
        body = CSRF_TOKEN.sub(CSRF_PLACEHOLDER, body)
        if CSRF_TOKEN.search(body):
            return response
        qcontext = getattr(response, "qcontext", None) or {}
        template = qcontext.get("response_template")
        main_object = qcontext.get("main_object")
        path = request.httprequest.path
        meta = {
            "headers": [(k, v) for k, v in response.headers.items() if k in KEPT_HEADERS],
            "template": template if isinstance(template, str) else None,
            "created": time.time(),
            "snapshot": ctx["snapshot"],
            "deps": cls._page_cache_dependencies(path, body, main_object),
            "path": path,
            "host": request.httprequest.host,
            "lang": request.lang.code,
            "website_id": ctx["website_id"],
        }
        if getattr(main_object, "_name", None) == "blog.post" and BLOG_POST_PATH.match(path):
            meta["blog_post_id"] = main_object.id
        stats.count(request.db, ctx["website_id"], misses=1, miss_time=render_time)
        if store.save(ctx["backend"], request.db, ctx["key"], meta, body):
            stats.count(request.db, ctx["website_id"], stored=1)
            response.headers["X-Page-Cache"] = "MISS"
            if random.random() < 0.01:
                store.trim(ctx["backend"], request.db, ctx["max_bytes"])
        return response

    @classmethod
    def _page_cache_clear(cls):
        """Drop every cached page of the current database (for scripts and the shell)."""
        store.clear_all(request.db)
