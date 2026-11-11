# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import shutil
import tempfile
from unittest.mock import Mock, patch

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged

from .. import changes, fit, settings, stats, store, triggers
from ..models.ir_http import IrHttp


@tagged("post_install", "-at_install")
class TestPageCache(TransactionCase):
    def test_store_roundtrip_in_both_backends(self):
        tmp = tempfile.mkdtemp()
        try:
            with patch.object(store, "root", lambda backend, dbname=None: f"{tmp}/{backend}/{dbname or ''}"):
                for backend in store.BACKENDS:
                    key = [1, "host", "de_DE", "/shop", 0, 0, False]
                    self.assertIsNone(store.load(backend, "db", key))
                    self.assertTrue(store.save(backend, "db", key, {"deps": ["a:1"]}, b"<html>x</html>"))
                    meta, body, path = store.load(backend, "db", key)
                    self.assertEqual(body, b"<html>x</html>")
                    self.assertEqual(meta["deps"], ["a:1"])
                    self.assertEqual(store.usage(backend, "db")[0], 1)
                    self.assertTrue(store.drop(path))
                    self.assertIsNone(store.load(backend, "db", key))
                    self.assertEqual(store.usage(backend, "db")[0], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_store_trim_removes_oldest(self):
        tmp = tempfile.mkdtemp()
        try:
            with patch.object(store, "root", lambda backend, dbname=None: f"{tmp}/{dbname or ''}"):
                for i in range(10):
                    store.save("file", "db", ["k", i], {}, bytes(range(256)) * 40)
                self.assertGreater(store.trim("file", "db", 1), 0)
                self.assertEqual(store.usage("file", "db")[0], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_snapshot_staleness(self):
        index = changes.Index()
        index.add("product.template:5", 100)
        index.add("product.template:6", 120)
        deps = ["product.template:5"]
        # change committed before the render started: the page already contains it
        self.assertFalse(index.stale(deps, changes.parse_snapshot("110:130:")))
        # change was still running when the page was rendered (xip): the page may miss it
        self.assertTrue(index.stale(deps, changes.parse_snapshot("90:130:100")))
        # change started after the render
        self.assertTrue(index.stale(deps, changes.parse_snapshot("90:100:")))
        # other products do not matter
        self.assertFalse(index.stale(["product.template:7"], changes.parse_snapshot("90:100:")))

    def test_triggers_write_the_journal(self):
        cr = self.env.cr
        triggers.ensure_triggers(cr)
        self.assertFalse(triggers.missing_tables(cr))
        cr.execute(f"SELECT coalesce(max(id), 0) FROM {triggers.LOG_TABLE}")
        last = cr.fetchone()[0]
        template = self.env["product.template"].create({"name": "page cache probe"})
        template.name = "page cache probe 2"
        template.flush_recordset()
        cr.execute(f"SELECT DISTINCT model, res_id FROM {triggers.LOG_TABLE} WHERE id > %s", [last])
        rows = set(cr.fetchall())
        self.assertIn(("product.template", template.id), rows)

    def test_visits_only_update_is_ignored(self):
        cr = self.env.cr
        triggers.ensure_triggers(cr)
        blog = self.env["blog.blog"].create({"name": "page cache blog"})
        post = self.env["blog.post"].create({"name": "page cache post", "blog_id": blog.id})
        cr.execute(f"SELECT coalesce(max(id), 0) FROM {triggers.LOG_TABLE}")
        last = cr.fetchone()[0]
        cr.execute("UPDATE blog_post SET visits = coalesce(visits, 0) + 1 WHERE id = %s", [post.id])
        cr.execute(
            f"SELECT count(*) FROM {triggers.LOG_TABLE} WHERE id > %s AND model = 'blog.post' AND res_id = %s",
            [last, post.id])
        self.assertEqual(cr.fetchone()[0], 0)
        post.name = "changed"
        post.flush_recordset()
        cr.execute(
            f"SELECT count(*) FROM {triggers.LOG_TABLE} WHERE id > %s AND model = 'blog.post' AND res_id = %s",
            [last, post.id])
        self.assertGreater(cr.fetchone()[0], 0)

    def test_fit_verdicts(self):
        with patch.object(store, "ram_available", return_value=True), patch.object(
            fit, "count_pages", return_value=[{"pages": 1000}]
        ), patch.object(fit, "meminfo", return_value={"available": 10**12}), patch.object(
            fit, "tmpfs_info", return_value={"size": 10**12}
        ), patch.object(store, "average_size", return_value=1000):
            budget = 10**6  # need = 1000 * 1000 = 1 MB
            self.assertEqual(fit.estimate(self.env, "db", budget * 2)["verdict"], "fits")
            self.assertEqual(fit.estimate(self.env, "db", int(budget * 1.1))["verdict"], "tight")
            self.assertEqual(fit.estimate(self.env, "db", budget // 2)["verdict"], "too_big")
            self.assertEqual(fit.estimate(self.env, "db", budget * 2)["shortfall"],
                             {"budget": 0, "tmpfs": 0, "memory": 0})
            self.assertEqual(fit.estimate(self.env, "db", budget // 2)["shortfall"]["budget"], int(budget / 0.8) - budget // 2)
        with patch.object(store, "ram_available", return_value=True), patch.object(
            fit, "count_pages", return_value=[{"pages": 1000}]
        ), patch.object(fit, "meminfo", return_value={"available": 2 * 10**6}), patch.object(
            fit, "tmpfs_info", return_value={"size": 10**6}
        ), patch.object(store, "average_size", return_value=1000):
            short = fit.estimate(self.env, "db", 10**7)["shortfall"]
            self.assertEqual(short["budget"], 0)
            self.assertEqual(short["tmpfs"], 250000)  # 1 MB / 0.8 - 1 MB
            self.assertEqual(short["memory"], 500000)  # 2 * 1.25 MB - 2 MB
        with patch.object(store, "ram_available", return_value=False):
            self.assertEqual(fit.estimate(self.env, "db", 10**9)["verdict"], "unavailable")

    def test_dashboard_settings_and_views(self):
        dash = self.env["website.page.cache.dashboard"].create({})
        dash.write({"ttl_hours": 6, "backend": "file", "pages": False, "exclude": "/contactus\n/event/*"})
        self.env["ir.config_parameter"].flush_model()
        self.assertEqual(settings.read_params(self.env.cr)["ttl_hours"], "6.0")
        self.assertEqual(settings.read_params(self.env.cr)["backend"], "file")
        self.assertEqual(settings.read_params(self.env.cr)["pages"], "0")
        self.assertEqual(settings.read_params(self.env.cr)["exclude"], "/contactus\n/event/*")
        self.assertEqual(settings.read_params(self.env.cr)["enabled"], "1")  # untouched settings stay as they were
        self.assertIn("<table", dash.stats_html)
        self.assertIn("<table", dash.fit_html)
        self.assertTrue(dash.journal_ok)


    def test_saving_counters_of_a_hit(self):
        meta = {"size_original": 1000, "size_stored": 800}
        # token longer than its placeholder by 10 bytes: the original grows by the same amount
        self.assertEqual(
            IrHttp._page_cache_saving(meta, 800, 810, 300),
            {"hits_sized": 1, "bytes_original": 1010, "bytes_minified": 810, "bytes_sent": 300})
        for old in ({}, {"size_original": None}, {"size_original": 0}, {"size_original": "x"}, {"size_original": True}):
            self.assertEqual(IrHttp._page_cache_saving(old, 800, 810, 300), {"hits_unsized": 1})

    def test_saving_counters_are_flushed_and_old_rows_stay_valid(self):
        website = self.env["website"].search([], limit=1)
        self.env["website.page.cache.stat"].flush_model()
        self.env.cr.execute(
            "INSERT INTO website_page_cache_stat (date, website_id, pid, hits) VALUES ('2000-01-01', %s, 1, 5)",
            [website.id])  # a row of the old schema: new columns must default to 0
        self.env.cr.execute("SELECT hits_sized, bytes_original, asset_sent FROM website_page_cache_stat WHERE pid = 1")
        self.assertEqual(self.env.cr.fetchone(), (0, 0.0, 0.0))
        sql = stats.upsert_sql()
        for name in stats.SAVING_FIELDS:
            self.assertIn(f"{name} = website_page_cache_stat.{name} + EXCLUDED.{name}", sql)
        self.assertEqual(sql.count("%s"), 3 + len(stats.FIELDS))
        values = dict(dict.fromkeys(stats.FIELDS, 0), hits=2, hits_sized=2, bytes_original=3000.0, bytes_minified=2400.0,
                               bytes_sent=900.0, asset_hits=1, asset_original=500.0, asset_sent=100.0)
        for _ in range(2):  # the second run takes the ON CONFLICT path
            self.env.cr.execute(sql, ["2000-01-02", website.id, 1] + [values[f] for f in stats.FIELDS])
        self.env.cr.execute("SELECT bytes_original, bytes_sent, asset_hits FROM website_page_cache_stat "
                            "WHERE date = '2000-01-02' AND pid = 1")
        self.assertEqual(self.env.cr.fetchone(), (6000.0, 1800.0, 2))

    def test_asset_counters_ignore_old_copies(self):
        from ..controllers import binary

        with patch.object(binary, "request", Mock(db="db")), patch.object(binary.stats, "count") as count, patch.object(
            binary.stats, "maybe_flush"
        ):
            binary._asset_count({"size_original": 1000, "website_id": 1}, b"x" * 300)
            count.assert_called_once_with("db", 1, asset_hits=1, asset_original=1000, asset_sent=300)
            count.reset_mock()
            binary._asset_count({"content_type": "text/css"}, b"x" * 300)  # copy made before the update
            binary._asset_count({"size_original": 1000}, b"x" * 300)
            count.assert_not_called()

    def test_dashboard_shows_savings_and_hides_unsized(self):
        website = self.env["website"].search([], limit=1)
        today = fields.Date.today()
        self.env["website.page.cache.stat"].sudo().create({
            "date": today, "website_id": website.id, "pid": 987654, "hits": 10, "hits_sized": 8, "hits_unsized": 2,
            "bytes_original": 8000.0, "bytes_minified": 6000.0, "bytes_sent": 2000.0,
            "asset_hits": 4, "asset_original": 4000.0, "asset_sent": 1000.0})
        html = self.env["website.page.cache.dashboard"].create({}).stats_html
        self.assertIn("Saved by minify and compression", html)
        self.assertIn("75.0 %", html)  # (8000 - 2000) / 8000, also the assets
        self.assertIn("2 hits of pages stored before the update", html)

    def test_exclude_patterns(self):
        patterns = ["/contactus", "/event/*", "# comment"]
        self.assertTrue(settings.excluded(patterns, "/contactus"))
        self.assertTrue(settings.excluded(patterns, "/event/summer-2026"))
        self.assertFalse(settings.excluded(patterns, "/contactus-thank-you"))
        self.assertFalse(settings.excluded(patterns, "/"))

    def test_host_key_is_normalized(self):
        from ..models.ir_http import IrHttp

        def key(canonical, indexable, host="h", normalize=True):
            website = Mock()
            website._is_canonical_url.return_value = canonical
            website._is_indexable_url.return_value = indexable
            req = Mock(httprequest=Mock(host=host, url_root="https://%s/" % host))
            with patch("odoo.addons.website_page_cache.models.ir_http.request", req):
                return IrHttp._page_cache_host_key(website, normalize)

        self.assertEqual(key(True, True, "www.example.com"), "canonical")
        # foreign hosts share one entry (they all carry the noindex page) ...
        self.assertEqual(key(False, False, "a.example.net"), key(False, False, "b.example.org"))
        # ... which is neither the canonical page nor the indexable non-canonical one (http://, bare domain)
        self.assertNotEqual(key(False, False), key(False, True))
        self.assertNotEqual(key(True, True), key(False, True))
        self.assertNotEqual(key(True, True), key(False, False))
        # switched off: the raw host as before
        self.assertEqual(key(False, False, "a.example.net", normalize=False), "a.example.net")
        self.assertEqual(settings.DEFAULTS["normalize_host"], "1")

        # a failing canonical check falls back to the raw host instead of sharing anything
        website = Mock()
        website._is_canonical_url.side_effect = RuntimeError
        req = Mock(httprequest=Mock(host="x.example.net"))
        with patch("odoo.addons.website_page_cache.models.ir_http.request", req), self.assertLogs(
            "odoo.addons.website_page_cache.models.ir_http", "ERROR"
        ):
            self.assertEqual(IrHttp._page_cache_host_key(website, True), "x.example.net")


@tagged("post_install", "-at_install")
class TestBuilderPages(HttpCase):
    def test_home_page_is_cached_and_follows_settings(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        dbname = self.env.cr.dbname
        self.addCleanup(settings.reset, dbname)

        def params(**override):
            values = dict(settings.DEFAULTS, backend="file", **override)
            return patch.object(settings, "read_params", lambda cr: values)

        with patch.object(store, "root", lambda backend, dbname=None: f"{tmp}/{backend}/{dbname or ''}"):
            with params():
                settings.reset(dbname)
                self.assertEqual(self.url_open("/").headers.get("X-Page-Cache"), "MISS")
                # a HIT cannot be shown here: rendering writes asset attachments inside the test
                # transaction, which stays "in progress" and marks the stored page stale
                self.assertEqual(len(list(store.entries("file", dbname))), 1)
            with params(exclude="/"):
                settings.reset(dbname)
                self.assertIsNone(self.url_open("/").headers.get("X-Page-Cache"))
            with params(pages="0"):
                settings.reset(dbname)
                self.assertIsNone(self.url_open("/").headers.get("X-Page-Cache"))
