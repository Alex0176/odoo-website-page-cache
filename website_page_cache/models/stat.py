# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models


class PageCacheStat(models.Model):
    _name = "website.page.cache.stat"
    _description = "Page cache statistics"
    _order = "date desc, website_id, pid"

    date = fields.Date(required=True, index=True)
    website_id = fields.Many2one("website", required=True, ondelete="cascade", index=True)
    pid = fields.Integer(string="Process", required=True)
    hits = fields.Integer(help="Pages served from the cache")
    misses = fields.Integer(help="Cacheable pages that had to be rendered")
    stored = fields.Integer(help="Rendered pages that were written to the cache")
    stale = fields.Integer(string="Invalidated", help="Cached pages thrown away because content changed or expired")
    hit_time = fields.Float(string="Time spent on hits (s)")
    miss_time = fields.Float(string="Time spent rendering misses (s)")
    bytes_served = fields.Float(string="Bytes served from cache")

    _date_website_pid = models.Constraint(
        "UNIQUE (date, website_id, pid)", "One statistics row per day, website and process.")
