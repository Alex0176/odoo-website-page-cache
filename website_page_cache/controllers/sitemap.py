# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Keep the sitemap away from visitors and crawlers.

Odoo stores the generated sitemap as an attachment but regenerates it inside the request that finds
it older than 12 hours. On a site with many posts and languages that takes 20 to 40 seconds and
blocks a worker, and every crawler arriving in that window waits for it (or starts a second run).

Here the stored sitemap is served as long as it is younger than ``website_page_cache.sitemap_max_hours``
(default 36). A cron job (``website.page.cache.service._cron_sitemaps``) renews it in the background
before that, by calling ``/sitemap.xml`` with the secret header ``X-Sitemap-Refresh``. In that call the
stored copy is back-dated inside the transaction, so the core code regenerates it, and everybody else
keeps reading the old copy until the new one is committed.
"""
import base64
import datetime
import hmac
from hashlib import md5

from odoo import http
from odoo.http import request

from odoo.addons.website.controllers.main import Website

PARAM_MAX_HOURS = "website_page_cache.sitemap_max_hours"
PARAM_SECRET = "website_page_cache.sitemap_secret"
DEFAULT_MAX_HOURS = 36.0
REFRESH_HEADER = "X-Sitemap-Refresh"


def sitemap_url(website_id, url_root):
    """URL of the stored sitemap attachment; the same formula as the core controller."""
    return "/sitemap-%d-%s.xml" % (website_id, md5(url_root.encode()).hexdigest()[:8])


class PageCacheSitemap(Website):
    @http.route()
    def sitemap_xml_index(self, **kwargs):
        icp = request.env["ir.config_parameter"].sudo()
        attachment = request.env["ir.attachment"].sudo().search(
            [("url", "=", sitemap_url(request.website.id, request.httprequest.url_root)), ("type", "=", "binary")],
            limit=1)
        if attachment:
            secret = icp.get_param(PARAM_SECRET) or ""
            given = request.httprequest.headers.get(REFRESH_HEADER, "")
            if secret and given and hmac.compare_digest(secret, given):
                # back-dated only in this transaction: the core code now regenerates the sitemap
                request.env.cr.execute(
                    "UPDATE ir_attachment SET create_date = create_date - interval '2 days' WHERE id = %s",
                    [attachment.id])
                attachment.invalidate_recordset(["create_date"])
            else:
                try:
                    max_hours = float(icp.get_param(PARAM_MAX_HOURS, DEFAULT_MAX_HOURS))
                except (TypeError, ValueError):
                    max_hours = DEFAULT_MAX_HOURS
                age = datetime.datetime.now() - attachment.create_date
                if max_hours > 0 and age < datetime.timedelta(hours=max_hours):
                    return request.make_response(
                        base64.b64decode(attachment.datas), [("Content-Type", "application/xml;charset=utf-8")])
        return super().sitemap_xml_index(**kwargs)
