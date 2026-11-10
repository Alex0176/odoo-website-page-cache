# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Long browser cache for the images of companies and contacts (logos, icons, avatars).

Odoo only sends a max-age for ``/web/image`` when the URL carries a ``unique`` hash; the plain
URLs (``/web/image/res.partner/42/image_128``) get ``no-cache`` and every page view revalidates
them. For these images the response gets ``Cache-Control: max-age=<days>`` instead. A changed
image still shows up with the next visit after the max-age, the ETag keeps revalidation cheap.
Parameter ``website_page_cache.image_days`` (default 30, 0 = leave Odoo's headers alone).
"""
from odoo.http import request
from odoo.addons.web.controllers.binary import Binary

IMAGE_MODELS = ("res.partner", "res.company")
DEFAULT_DAYS = 30


class PageCacheBinary(Binary):
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
