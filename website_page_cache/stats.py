# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Per-process counters, flushed to ``website.page.cache.stat`` at most once a minute.

Each process writes only its own rows (keyed by pid), so concurrent flushes never conflict, and
a separate cursor is used so the statistics never ride on the visitor's transaction.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone

from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

FLUSH_SECONDS = 60
BASE_FIELDS = ("hits", "misses", "stored", "stale", "hit_time", "miss_time", "bytes_served")
# Savings (19.0.1.4.0): only for hits whose entry knows its original size; ``hits_unsized`` counts the others.
SAVING_FIELDS = ("hits_sized", "hits_unsized", "bytes_original", "bytes_minified", "bytes_sent",
                 "asset_hits", "asset_original", "asset_sent")
FIELDS = BASE_FIELDS + SAVING_FIELDS

_lock = threading.Lock()
_counters = {}  # (dbname, website_id) -> {field: number}
_last_flush = {}  # dbname -> monotonic time


def count(dbname, website_id, **values):
    with _lock:
        bucket = _counters.setdefault((dbname, website_id), dict.fromkeys(FIELDS, 0))
        for name, value in values.items():
            bucket[name] += value


def maybe_flush(dbname):
    now = time.monotonic()
    if now - _last_flush.setdefault(dbname, now) < FLUSH_SECONDS:
        return
    flush(dbname)


def upsert_sql():
    columns = ", ".join(FIELDS)
    marks = ", ".join(["%s"] * len(FIELDS))
    updates = ",\n".join(f"{f} = website_page_cache_stat.{f} + EXCLUDED.{f}" for f in FIELDS)
    return (
        f"INSERT INTO website_page_cache_stat (date, website_id, pid, {columns}, create_date, write_date) "
        f"VALUES (%s, %s, %s, {marks}, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc') "
        f"ON CONFLICT (date, website_id, pid) DO UPDATE SET {updates}, write_date = now() AT TIME ZONE 'utc'")


def flush(dbname):
    with _lock:
        _last_flush[dbname] = time.monotonic()
        taken = {k: v for k, v in _counters.items() if k[0] == dbname}
        for key in taken:
            del _counters[key]
    if not taken:
        return
    today = datetime.now(timezone.utc).date()
    try:
        with Registry(dbname).cursor() as cr:
            for (_db, website_id), values in taken.items():
                cr.execute(upsert_sql(), [today, website_id, os.getpid()] + [values[f] for f in FIELDS])
    except Exception:
        _logger.exception("page cache: cannot write statistics")
        with _lock:  # keep the numbers for the next attempt
            for (db, website_id), values in taken.items():
                bucket = _counters.setdefault((db, website_id), dict.fromkeys(FIELDS, 0))
                for name, value in values.items():
                    bucket[name] += value
