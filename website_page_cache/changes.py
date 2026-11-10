# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""In-process index of the change journal, and the staleness test for cache entries.

A cached page records the PostgreSQL snapshot it was rendered with. A journal row whose
transaction is *not visible* in that snapshot (it committed after, or was still running) may have
changed something the page shows, so a page that depends on its tag is stale. Journal rows below
the snapshot's ``xmin`` are known to be reflected in the page.
"""
import logging
import threading
import time
from bisect import bisect_left

from .triggers import LOG_TABLE

_logger = logging.getLogger(__name__)

REFRESH_SECONDS = 2
REBUILD_SECONDS = 3600
GLOBAL_TAG = "*:0"

_lock = threading.Lock()
_indexes = {}  # dbname -> Index


def parse_snapshot(text):
    """``'xmin:xmax:xip1,xip2'`` -> ``(xmin, xmax, frozenset(xip))``."""
    xmin, xmax, xip = text.split(":")
    return int(xmin), int(xmax), frozenset(int(x) for x in xip.split(",") if x)


def current_snapshot(cr):
    cr.execute("SELECT pg_current_snapshot()::text")
    return cr.fetchone()[0]


class Index:
    def __init__(self):
        self.by_tag = {}
        self.last_xmin = None
        self.checked = 0.0
        self.built = 0.0
        self.rows = 0

    def add(self, tag, xid):
        xs = self.by_tag.setdefault(tag, [])
        i = bisect_left(xs, xid)
        if i == len(xs) or xs[i] != xid:
            xs.insert(i, xid)
            self.rows += 1

    def stale(self, deps, snapshot):
        xmin, xmax, xip = snapshot
        for tag in deps:
            xs = self.by_tag.get(tag)
            if not xs:
                continue
            if xs[-1] >= xmax:
                return True
            for x in xip:
                i = bisect_left(xs, x)
                if i < len(xs) and xs[i] == x:
                    return True
        return False


def refresh(cr, dbname, keep_seconds):
    """Bring the index of a database up to date (at most every ``REFRESH_SECONDS``)."""
    index = _indexes.get(dbname)
    now = time.monotonic()
    if index and now - index.checked < REFRESH_SECONDS and now - index.built < REBUILD_SECONDS:
        return index
    with _lock:
        index = _indexes.get(dbname)
        now = time.monotonic()
        if index and now - index.checked < REFRESH_SECONDS and now - index.built < REBUILD_SECONDS:
            return index
        rebuild = index is None or now - index.built >= REBUILD_SECONDS
        fresh = Index() if rebuild else index
        snapshot_xmin = parse_snapshot(current_snapshot(cr))[0]
        if rebuild:
            cr.execute(
                f"SELECT model, res_id, xid FROM {LOG_TABLE} "
                "WHERE create_date > (now() AT TIME ZONE 'utc') - %s * interval '1 second'", [keep_seconds])
            fresh.built = now
        else:
            cr.execute(f"SELECT model, res_id, xid FROM {LOG_TABLE} WHERE xid >= %s", [fresh.last_xmin])
        for model, res_id, xid in cr.fetchall():
            fresh.add(f"{model}:{res_id}", xid)
        fresh.last_xmin = snapshot_xmin
        fresh.checked = now
        _indexes[dbname] = fresh
        return fresh


def forget(dbname):
    _indexes.pop(dbname, None)


def info(dbname):
    index = _indexes.get(dbname)
    return {"tags": len(index.by_tag), "rows": index.rows} if index else {"tags": 0, "rows": 0}
