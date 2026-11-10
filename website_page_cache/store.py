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

