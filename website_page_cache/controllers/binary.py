# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Long browser cache for the images of companies and contacts (logos, icons, avatars).

Odoo only sends a max-age for ``/web/image`` when the URL carries a ``unique`` hash; the plain
URLs (``/web/image/res.partner/42/image_128``) get ``no-cache`` and every page view revalidates
them. For these images the response gets ``Cache-Control: max-age=<days>`` instead. A changed
image still shows up with the next visit after the max-age, the ETag keeps revalidation cheap.
Parameter ``website_page_cache.image_days`` (default 30, 0 = leave Odoo's headers alone).

``/web/assets/...`` bundles (versioned URLs, immutable) are answered by Odoo uncompressed. For clients that accept
gzip the module compresses each bundle once, keeps the copy on disk (``page_cache/assets_gz``) and sends it from
there afterwards (parameter ``website_page_cache.compress``).
"""
import logging
import re

from odoo.http import Response, request, route
from odoo.addons.web.controllers.binary import ANY_UNIQUE, Binary
from odoo.addons.website.controllers.binary import WebsiteBinary

from .. import compress, settings, stats, store

_logger = logging.getLogger(__name__)

IMAGE_MODELS = ("res.partner", "res.company")
DEFAULT_DAYS = 30
ASSET_TYPES = ("text/css", "text/javascript", "application/javascript")


def _asset_key(unique, nocache):
    """The URL as key, when gzip is wanted and allowed; ``None`` otherwise."""
    httprequest = request.httprequest
    if (
        nocache
        or not request.db
        or httprequest.method != "GET"
        or httprequest.headers.get("Range")
        or unique in (None, "", "any", "%", "debug", ANY_UNIQUE)
        or not compress.accepts_gzip(httprequest.headers.get("Accept-Encoding"))
    ):
        return None
    if not settings.get(request.env.cr, request.db)["compress"]:
        return None
    return httprequest.full_path


def _asset_response(meta, data):
    headers = [("Content-Type", meta["content_type"]), ("Content-Encoding", "gzip"),
               ("Content-Length", str(len(data))), ("Vary", "Accept-Encoding")]
    if meta.get("cache_control"):
        headers.append(("Cache-Control", meta["cache_control"]))
    response = Response(data, status=200, headers=headers)
    response.headers["X-Page-Cache"] = "GZ"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


ASSET_WEBSITE = re.compile(r"^/web/assets/(\d+)/")


def _asset_website_id(path):
    """The website of ``/web/assets/<website_id>/...`` if it exists (a statistics row needs a real website)."""
    match = ASSET_WEBSITE.match(path)
    if not match:
        return None
    return request.env["website"].sudo().browse(int(match.group(1))).exists().id or None


def _asset_count(meta, data):
    """Original (uncompressed) vs. sent bytes of a gzip copy; copies made before 19.0.1.4.0 are not counted."""
    original = meta.get("size_original")
    website_id = meta.get("website_id")
    if not isinstance(original, (int, float)) or isinstance(original, bool) or original <= 0 or not website_id:
        return
    stats.count(request.db, website_id, asset_hits=1, asset_original=original, asset_sent=len(data))
    stats.maybe_flush(request.db)


def _asset_store(key, response):
    if (
        not isinstance(response, Response)
        or response.status_code != 200
        or response.headers.get("X-Accel-Redirect")
        or response.headers.get("Content-Encoding")
        or response.mimetype not in ASSET_TYPES
    ):
        return response
    if response.direct_passthrough:  # files from the filestore are streamed; read them into memory here
        response.direct_passthrough = False
    raw = response.get_data()
    if len(raw) < compress.MIN_SIZE:
        return response
    data = compress.gzip_bytes(raw)
    meta = {"content_type": response.headers.get("Content-Type"),
            "cache_control": response.headers.get("Cache-Control"), "size_original": len(raw),
            "website_id": _asset_website_id(request.httprequest.path)}
    if len(data) >= len(raw) or not store.asset_save(request.db, key, meta, data):
        return response
    return _asset_response(meta, data)


def serve_asset(unique, nocache, render):
    """The gzip copy of an asset if there is one, else ``render()``'s answer (and a copy made from it)."""
    key = None
    try:
        key = _asset_key(unique, nocache)
        if key:
            found = store.asset_load(request.db, key)
            if found:
                _asset_count(*found)
                return _asset_response(*found)
    except Exception:
        _logger.exception("page cache: asset lookup failed")
        key = None
    response = render()
    if key:
        try:
            response = _asset_store(key, response)
        except Exception:
            _logger.exception("page cache: asset compression failed")
    return response


class PageCacheWebsiteBinary(WebsiteBinary):
    @route()  # /web/assets/<website_id>/<unique>/<filename>, the URL the pages use
    def content_assets_website(self, website_id=None, **kwargs):
        return serve_asset(kwargs.get("unique"), kwargs.get("nocache"),
                           lambda: super(PageCacheWebsiteBinary, self).content_assets_website(
                               website_id=website_id, **kwargs))


class PageCacheBinary(Binary):
    @route()  # keep the parent's route unchanged
    def content_assets(self, filename=None, unique=ANY_UNIQUE, nocache=False, assets_params=None):
        """gzip copy of the (immutable, versioned) asset bundles, made once and kept on disk."""
        return serve_asset(unique, nocache, lambda: super(PageCacheBinary, self).content_assets(
            filename=filename, unique=unique, nocache=nocache, assets_params=assets_params))

    @route()  # keep the parent's route unchanged
    def content_image(self, xmlid=None, model="ir.attachment", id=None, field="raw", unique=False,
                      nocache=False, download=False, **kw):
        response = super().content_image(
            xmlid=xmlid, model=model, id=id, field=field, unique=unique, nocache=nocache, download=download, **kw)
        try:
            self._page_cache_image_headers(response, model, field, unique, nocache, download)
        except Exception:  # headers are an optimisation, never break the image
            pass
        return response

    @staticmethod
    def _page_cache_image_headers(response, model, field, unique, nocache, download):
        if model not in IMAGE_MODELS or unique or nocache or download:
            return
        if response.status_code != 200 or (response.cache_control.max_age or 0) > 0:
            return
        if not (field.startswith(("image_", "avatar_")) or field in ("logo", "favicon", "image")):
            return
        try:
            days = float(request.env["ir.config_parameter"].sudo().get_param(
                "website_page_cache.image_days", DEFAULT_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        if days <= 0:
            return
        response.cache_control.pop("no-cache", None)
        response.cache_control.max_age = int(days * 86400)
