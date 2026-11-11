# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import os
import shutil
import tempfile
import time
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from .. import changes, store


@tagged("post_install", "-at_install")
class TestMirror(BaseCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = patch.object(store, "root", lambda backend, dbname=None: f"{self.tmp}/{backend}/{dbname or ''}")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(store, "mirror_root", lambda dbname: f"{self.tmp}/mirror/{dbname}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self, i, body=b"<html>page</html>", **meta):
        key = ["k", i]
        self.assertTrue(store.save("ram", "db", key, dict({"deps": ["a:1"]}, **meta), body))
        return key

    def test_sync_is_incremental_and_follows_deletions(self):
        keys = [self._save(i) for i in range(5)]
        self.assertEqual(store.mirror_sync("db", 10**6), (5, 0))
        self.assertEqual(store.mirror_sync("db", 10**6), (0, 0))  # nothing new: nothing copied
        # a changed entry is copied again, the rest is left alone
        time.sleep(0.02)
        self._save(2, body=b"<html>changed</html>")
        self.assertEqual(store.mirror_sync("db", 10**6), (1, 0))
        # a dropped entry disappears from the mirror
        _meta, _body, path = store.load("ram", "db", keys[0])
        store.drop(path)
        self.assertEqual(store.mirror_sync("db", 10**6), (0, 1))
        self.assertEqual(len(store._tree(store.mirror_root("db"))), 4)

    def test_sync_respects_the_size_limit(self):
        for i in range(6):
            self._save(i, body=os.urandom(2000))
        total = sum(size for size, _m in store._tree(store.root("ram", "db")).values())
        store.mirror_sync("db", total // 2)
        kept = sum(size for size, _m in store._tree(store.mirror_root("db")).values())
        self.assertLessEqual(kept, total // 2)
        self.assertGreater(kept, 0)

    def test_restore_after_reboot(self):
        keys = [self._save(i, body=b"<p>%d</p>" % i) for i in range(4)]
        store.mirror_sync("db", 10**6)
        shutil.rmtree(store.root("ram", "db"))  # the host was rebooted
        self.assertEqual(store.mirror_restore("db"), 4)
        for i, key in enumerate(keys):
            self.assertEqual(store.load("ram", "db", key)[1], b"<p>%d</p>" % i)
        self.assertFalse(os.path.exists(os.path.join(store.mirror_root("db"), store.RESTORE_MARKER)))
        # RAM is filled now: a second call does not start another restore
        self.assertEqual(store.mirror_restore("db"), 0)

    def test_unfinished_restore_blocks_pruning_until_done(self):
        for i in range(4):
            self._save(i)
        store.mirror_sync("db", 10**6)
        shutil.rmtree(store.root("ram", "db"))
        self.assertEqual(store.mirror_restore("db", max_seconds=-1), 0)  # out of time at once
        marker = os.path.join(store.mirror_root("db"), store.RESTORE_MARKER)
        self.assertTrue(os.path.exists(marker))
        # RAM is still empty, but the mirror must not be emptied to match it
        self.assertEqual(store.mirror_sync("db", 10**6), (0, 0))
        self.assertEqual(len(store._tree(store.mirror_root("db"))), 4)
        self.assertEqual(store.mirror_restore("db", max_seconds=30), 4)
        self.assertFalse(os.path.exists(marker))

    def test_restored_entries_are_judged_like_any_other(self):
        key = self._save(1, created=time.time() - 100, snapshot="100:110:", deps=["product.template:5"])
        store.mirror_sync("db", 10**6)
        shutil.rmtree(store.root("ram", "db"))
        store.mirror_restore("db")
        meta, _body, _path = store.load("ram", "db", key)
        index = changes.Index()
        index.add("product.template:5", 150)  # changed after the page was rendered, while the host was down
        self.assertTrue(index.stale(meta["deps"], changes.parse_snapshot(meta["snapshot"])))
        index = changes.Index()
        index.add("product.template:9", 150)  # an unrelated change
        self.assertFalse(index.stale(meta["deps"], changes.parse_snapshot(meta["snapshot"])))
        self.assertGreater(time.time() - meta["created"], 60)  # the maximum age check uses the original time

    def test_clear_all_empties_the_mirror(self):
        self._save(1)
        store.mirror_sync("db", 10**6)
        self.assertTrue(store._tree(store.mirror_root("db")))
        store.clear_all("db")
        self.assertFalse(os.path.exists(store.mirror_root("db")))
        self.assertEqual(store.mirror_restore("db"), 0)  # nothing to bring back
