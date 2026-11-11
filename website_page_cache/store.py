# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Entry store shared by all Odoo processes of a database.

Two backends with identical layout (``<root>/<db>/<xx>/<sha1 of key>``):

* ``file`` - below the Odoo data directory (disk, helped by the operating system's page cache),
* ``ram``  - below ``/dev/shm`` (tmpfs): memory that all workers share, so the pages are held once
  and not once per process.

An entry is one header line (JSON: key, headers, dependencies, render snapshot, ...) followed by
the zlib-compressed body. Writes are atomic (temporary file + rename).
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import zlib

from odoo.tools import config

_logger = logging.getLogger(__name__)

BACKENDS = ("file", "ram")
RAM_BASE = "/dev/shm"
MAX_BODY = 3 * 1024 * 1024
MAX_HEADER = 256 * 1024


def ram_available():
    return os.path.isdir(RAM_BASE) and os.access(RAM_BASE, os.W_OK | os.X_OK)


def root(backend, dbname=None):
    if backend == "ram":
        # one directory per Odoo installation, so two instances on a host never share entries
        tag = hashlib.sha1(config["data_dir"].encode()).hexdigest()[:8]
        base = os.path.join(RAM_BASE, f"odoo-page-cache-{tag}")
    else:
        base = os.path.join(config["data_dir"], "page_cache")
    return os.path.join(base, dbname) if dbname else base


def _path(backend, dbname, key):
    digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return os.path.join(root(backend, dbname), digest[:2], digest)


def load(backend, dbname, key):
    """Return ``(meta, body, path)`` of an entry, or ``None``. No freshness check happens here."""
    path = _path(backend, dbname, key)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        head, _, payload = raw.partition(b"\n")
        meta = json.loads(head)
        if meta.get("key") != key:
            return None
        return meta, zlib.decompress(payload), path
    except (FileNotFoundError, ValueError, zlib.error):
        return None
    except OSError:
        _logger.exception("page cache: cannot read %s", path)
        return None


def save(backend, dbname, key, meta, body):
    if len(body) > MAX_BODY:
        return False
    path = _path(backend, dbname, key)
    meta = dict(meta, key=key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(json.dumps(meta).encode() + b"\n" + zlib.compress(body, 6))
        os.replace(tmp, path)
    except OSError:
        _logger.exception("page cache: cannot write %s", path)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def drop(path):
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        _logger.exception("page cache: cannot remove %s", path)
        return False


def entries(backend, dbname):
    """Yield ``(path, size, mtime)`` for every complete entry of a database."""
    base = root(backend, dbname)
    try:
        subs = list(os.scandir(base))
    except FileNotFoundError:
        return
    for sub in subs:
        if not sub.is_dir():
            continue
        try:
            files = list(os.scandir(sub.path))
        except FileNotFoundError:
            continue
        for entry in files:
            if entry.name.endswith(".tmp"):
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            yield entry.path, stat.st_size, stat.st_mtime


def read_meta(path):
    try:
        with open(path, "rb") as fh:
            head = fh.readline(MAX_HEADER)
        return json.loads(head)
    except (OSError, ValueError):
        return None


def usage(backend, dbname):
    """``(number of entries, bytes)``."""
    count = total = 0
    for _path_, size, _mtime in entries(backend, dbname):
        count += 1
        total += size
    return count, total


def average_size(backend, dbname, sample=400):
    """Average size of the newest entries, or ``None`` when the store is empty."""
    newest = sorted(entries(backend, dbname), key=lambda e: e[2], reverse=True)[:sample]
    return sum(e[1] for e in newest) / len(newest) if newest else None


def trim(backend, dbname, max_bytes):
    """Drop the oldest entries while the store is larger than ``max_bytes``; returns how many."""
    items = list(entries(backend, dbname))
    total = sum(e[1] for e in items)
    if total <= max_bytes:
        return 0
    removed = 0
    for path, size, _mtime in sorted(items, key=lambda e: e[2]):
        if total <= max_bytes * 0.8:
            break
        if drop(path):
            total -= size
            removed += 1
    return removed


def clear(backend, dbname):
    shutil.rmtree(root(backend, dbname), ignore_errors=True)


def clear_all(dbname):
    for backend in BACKENDS:
        clear(backend, dbname)
    asset_clear(dbname)
    mirror_clear(dbname)


# --- gzip copies of /web/assets files -------------------------------------------------------------
# <data_dir>/page_cache/assets_gz/<db>/<xx>/<sha1 of the URL>: one header line (JSON) + gzip data.
# Asset URLs carry a version hash, so a copy never goes stale; it is only removed when old or when
# the directory gets too large.
ASSET_DIR = "assets_gz"
ASSET_MAX_DAYS = 30
ASSET_MAX_BYTES = 256 * 1024 * 1024


def asset_root(dbname):
    return os.path.join(root("file"), ASSET_DIR, dbname)


def _asset_path(dbname, key):
    digest = hashlib.sha1(key.encode()).hexdigest()
    return os.path.join(asset_root(dbname), digest[:2], digest)


def asset_load(dbname, key):
    """``(meta, gzip data)`` or ``None``."""
    path = _asset_path(dbname, key)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        head, _, payload = raw.partition(b"\n")
        meta = json.loads(head)
        if meta.get("key") != key or not payload:
            return None
        return meta, payload
    except (OSError, ValueError):
        return None


def asset_save(dbname, key, meta, data):
    path = _asset_path(dbname, key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(json.dumps(dict(meta, key=key)).encode() + b"\n" + data)
        os.replace(tmp, path)
    except OSError:
        _logger.exception("page cache: cannot write %s", path)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def asset_entries(dbname):
    base = asset_root(dbname)
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            yield path, stat.st_size, stat.st_mtime


def asset_trim(dbname, max_days=ASSET_MAX_DAYS, max_bytes=ASSET_MAX_BYTES):
    """Remove copies older than ``max_days`` and, if still too large, the oldest ones; returns how many."""
    items = list(asset_entries(dbname))
    now = time.time()
    removed = 0
    keep = []
    for path, size, mtime in items:
        if path.endswith(".tmp"):
            if now - mtime > 3600 and drop(path):
                removed += 1
        elif now - mtime > max_days * 86400:
            removed += 1 if drop(path) else 0
        else:
            keep.append((path, size, mtime))
    total = sum(e[1] for e in keep)
    if total > max_bytes:
        for path, size, _mtime in sorted(keep, key=lambda e: e[2]):
            if total <= max_bytes * 0.8:
                break
            if drop(path):
                total -= size
                removed += 1
    return removed


def asset_clear(dbname):
    shutil.rmtree(asset_root(dbname), ignore_errors=True)



# --- disk mirror of the RAM backend ----------------------------------------------------------------
# tmpfs survives restarts of Odoo but not of the host. With ``persist`` on, the maintenance job copies
# new and changed RAM entries to <data_dir>/page_cache_mirror/<db>/ (same layout) and removes entries
# that are gone from RAM; after a host reboot they are copied back. Whether an entry is still valid is
# decided when it is loaded (age, journal snapshot) exactly as for any other entry. A marker file
# ``.restoring`` keeps the mirror from being pruned while a restore has not finished.
MIRROR_DIR = "page_cache_mirror"
RESTORE_MARKER = ".restoring"


def mirror_root(dbname):
    return os.path.join(config["data_dir"], MIRROR_DIR, dbname)


def _tree(base):
    """``{relative path: (size, mtime)}`` of the complete entries below ``base``."""
    found = {}
    try:
        subs = list(os.scandir(base))
    except FileNotFoundError:
        return found
    for sub in subs:
        if not sub.is_dir():
            continue
        try:
            files = list(os.scandir(sub.path))
        except FileNotFoundError:
            continue
        for entry in files:
            if entry.name.endswith(".tmp") or not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            found[os.path.join(sub.name, entry.name)] = (stat.st_size, stat.st_mtime)
    return found


def _copy(src, dst, mtime):
    tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, tmp)
        os.utime(tmp, (mtime, mtime))
        os.replace(tmp, dst)
        return True
    except OSError:
        _logger.warning("page cache: cannot copy %s to %s", src, dst, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def mirror_clear(dbname):
    shutil.rmtree(mirror_root(dbname), ignore_errors=True)


def mirror_sync(dbname, max_bytes, ram_backend="ram"):
    """Bring the mirror in line with the RAM backend: copy what is missing or newer (only that),
    remove what is gone, stay below ``max_bytes``. Returns ``(copied, removed)``. Does nothing while
    a restore is unfinished."""
    mirror = mirror_root(dbname)
    if os.path.exists(os.path.join(mirror, RESTORE_MARKER)):
        return 0, 0
    source = _tree(root(ram_backend, dbname))
    target = _tree(mirror)
    removed = 0
    for rel in set(target) - set(source):
        if drop(os.path.join(mirror, rel)):
            target.pop(rel)
            removed += 1
    total = sum(size for size, _m in target.values())
    copied = 0
    for rel, (size, mtime) in sorted(source.items(), key=lambda e: e[1][1], reverse=True):  # newest first
        known = target.get(rel)
        if known and known[1] >= mtime:
            continue
        if total - (known[0] if known else 0) + size > max_bytes:
            continue
        if _copy(os.path.join(root(ram_backend, dbname), rel), os.path.join(mirror, rel), mtime):
            total += size - (known[0] if known else 0)
            copied += 1
    return copied, removed


def mirror_restore(dbname, max_seconds=3.0, force=False, ram_backend="ram"):
    """Copy mirrored entries that are missing in RAM back. Starts only when the RAM directory of the
    database is empty (or ``force``); a restore that runs out of time leaves the marker so the
    maintenance job continues it. Returns the number of entries copied."""
    mirror = mirror_root(dbname)
    marker = os.path.join(mirror, RESTORE_MARKER)
    ram = root(ram_backend, dbname)
    if not os.path.isdir(mirror):
        return 0
    if not os.path.exists(marker):
        if not force and _tree(ram):
            return 0
        try:
            open(marker, "a").close()
        except OSError:
            return 0
    source = _tree(mirror)
    present = _tree(ram)
    deadline = time.monotonic() + max_seconds
    copied = 0
    for rel, (_size, mtime) in sorted(source.items(), key=lambda e: e[1][1], reverse=True):
        if rel in present:
            continue
        if time.monotonic() > deadline:
            return copied  # marker stays: the next maintenance run goes on
        if _copy(os.path.join(mirror, rel), os.path.join(ram, rel), mtime):
            copied += 1
    try:
        os.unlink(marker)
    except OSError:
        pass
    return copied
