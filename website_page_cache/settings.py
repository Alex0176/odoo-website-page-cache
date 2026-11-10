# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Settings in ``ir.config_parameter``, read at most every ``REFRESH_SECONDS`` per process."""
import threading
import time

from . import store
from .triggers import LOG_TABLE

PREFIX = "website_page_cache."
REFRESH_SECONDS = 30
DEFAULTS = {
    "enabled": "1",
    "backend": "auto",
    "auto_backend": "file",
    "ttl_hours": "12",
    "max_mb_file": "1024",
    "max_mb_ram": "256",
}

_lock = threading.Lock()
_state = {}  # dbname -> dict


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def read_params(cr):
    cr.execute("SELECT key, value FROM ir_config_parameter WHERE key LIKE %s", [PREFIX + "%"])
    params = {k[len(PREFIX):]: v for k, v in cr.fetchall()}
    return {name: params.get(name, default) for name, default in DEFAULTS.items()}


def effective_backend(params):
    wanted = params["backend"]
    if wanted == "auto":
        wanted = params["auto_backend"]
    if wanted == "ram" and not store.ram_available():
        wanted = "file"
    return wanted if wanted in store.BACKENDS else "file"


def get(cr, dbname):
    now = time.monotonic()
    st = _state.get(dbname)
    if st and now - st["checked"] < REFRESH_SECONDS:
        return st
    with _lock:
        st = _state.get(dbname)
        if st and now - st["checked"] < REFRESH_SECONDS:
            return st
        params = read_params(cr)
        cr.execute("SELECT to_regclass(%s)", [LOG_TABLE])
        backend = effective_backend(params)
        st = {
            "checked": now,
            "enabled": params["enabled"].strip().lower() not in ("0", "false", "no", ""),
            "ready": cr.fetchone()[0] is not None,
            "backend": backend,
            "ttl": max(60, int(_number(params["ttl_hours"], 12) * 3600)),
            "max_bytes": int(_number(params["max_mb_ram" if backend == "ram" else "max_mb_file"], 256) * 1048576),
        }
        _state[dbname] = st
        return st


def reset(dbname):
    _state.pop(dbname, None)
