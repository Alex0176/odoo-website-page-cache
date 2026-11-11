# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""gzip helpers for cached pages and assets."""
import gzip

MIN_SIZE = 860  # below roughly one packet gzip gains nothing


def accepts_gzip(header):
    """Does an ``Accept-Encoding`` header allow gzip (``gzip;q=0`` and a missing header do not)?"""
    for part in (header or "").lower().split(","):
        name, _, params = part.partition(";")
        if name.strip() not in ("gzip", "x-gzip"):
            continue
        params = params.replace(" ", "")
        if params.startswith("q="):
            try:
                return float(params[2:]) > 0
            except ValueError:
                return False
        return True
    return False


def gzip_bytes(data, level=6):
    return gzip.compress(data, level, mtime=0)


def add_vary(headers):
    """Add ``Vary: Accept-Encoding`` to a werkzeug ``Headers`` object."""
    current = headers.get("Vary")
    if not current:
        headers["Vary"] = "Accept-Encoding"
    elif "accept-encoding" not in current.lower() and current.strip() != "*":
        headers["Vary"] = f"{current}, Accept-Encoding"
