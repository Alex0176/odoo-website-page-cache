# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from odoo import fields, models

SAVING_COLUMNS = {
    "hits_sized": "integer", "hits_unsized": "integer", "bytes_original": "double precision",
    "bytes_minified": "double precision", "bytes_sent": "double precision", "asset_hits": "integer",
    "asset_original": "double precision", "asset_sent": "double precision",
}


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

    hits_sized = fields.Integer(
        string="Hits with size data", default=0,
        help="Hits of pages that know their original size (stored with version 19.0.1.4.0 or later)")
    hits_unsized = fields.Integer(
        string="Hits without size data", default=0,
        help="Hits of pages stored before the update; nothing is estimated for them")
    bytes_original = fields.Float(string="Bytes without optimisation", default=0,
                                  help="What the sized hits would have sent without minify and compression")
    bytes_minified = fields.Float(string="Bytes after minify", default=0)
    bytes_sent = fields.Float(string="Bytes sent (compressed)", default=0)
    asset_hits = fields.Integer(string="Asset hits (gzip)", default=0)
    asset_original = fields.Float(string="Asset bytes uncompressed", default=0)
    asset_sent = fields.Float(string="Asset bytes sent (gzip)", default=0)

    def _auto_init(self):
        # existing rows must stay valid: add the columns with DEFAULT 0 before the ORM looks at the table
        cr = self.env.cr
        cr.execute("SELECT to_regclass(%s)", [self._table])
        if cr.fetchone()[0]:
            for name, sql_type in SAVING_COLUMNS.items():
                cr.execute(f"ALTER TABLE {self._table} ADD COLUMN IF NOT EXISTS {name} {sql_type} NOT NULL DEFAULT 0")
        return super()._auto_init()

    _date_website_pid = models.Constraint(
        "UNIQUE (date, website_id, pid)", "One statistics row per day, website and process.")
