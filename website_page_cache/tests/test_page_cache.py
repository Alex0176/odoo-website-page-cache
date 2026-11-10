# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import shutil
import tempfile
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .. import changes, fit, settings, store, triggers


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
        dash.write({"ttl_hours": 6, "backend": "file"})
        self.env["ir.config_parameter"].flush_model()
        self.assertEqual(settings.read_params(self.env.cr)["ttl_hours"], "6.0")
        self.assertEqual(settings.read_params(self.env.cr)["backend"], "file")
        self.assertEqual(settings.read_params(self.env.cr)["enabled"], "1")  # untouched settings stay as they were
        self.assertIn("<table", dash.stats_html)
        self.assertIn("<table", dash.fit_html)
        self.assertTrue(dash.journal_ok)
