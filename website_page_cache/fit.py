# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Will the websites fit into a RAM cache? An estimate from the content, not a promise.

Pages = (products + shop list pages + categories + blog posts + blogs + 1 + website pages) x languages, per
website. The size of a page is measured from the entries already cached (average of the newest
400) or assumed to be ``DEFAULT_PAGE_BYTES``. The RAM cache is a tmpfs (``/dev/shm``) shared by all
workers, so the estimate is the total, not total x workers.
"""
import math
import os

from . import store

DEFAULT_PAGE_BYTES = 20 * 1024
SHOP_PAGE_SIZE = 20
SAFETY = 0.8  # use at most this share of the limit before saying "fits"


def meminfo():
    """``{'total': bytes, 'available': bytes}`` from /proc/meminfo (empty when unreadable)."""
    result = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    result["total" if name == "MemTotal" else "available"] = int(rest.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return result


def tmpfs_info():
    try:
        st = os.statvfs(store.RAM_BASE)
    except OSError:
        return {}
    return {"size": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}


def _count(env, model, domain):
    if model not in env:
        return 0
    return env[model].sudo().with_context(active_test=True).search_count(domain)


def count_pages(env):
    """Rows of ``{'website', 'languages', 'pages_per_language', 'pages', 'detail'}``."""
    rows = []
    for website in env["website"].sudo().search([]):
        multi = [False, website.id]
        products = _count(env, "product.template", [
            ("is_published", "=", True), ("sale_ok", "=", True), ("website_id", "in", multi)]
        ) if "is_published" in env["product.template"]._fields else 0
        categories = _count(env, "product.public.category", [("website_id", "in", multi)])
        posts = _count(env, "blog.post", [("is_published", "=", True), ("website_id", "in", multi)])
        blogs = _count(env, "blog.blog", [("website_id", "in", multi)])
        builder = _count(env, "website.page", [("is_published", "=", True), ("website_id", "in", multi)])
        ppg = getattr(website, "shop_ppg", 0) or SHOP_PAGE_SIZE
        shop_pages = math.ceil(products / ppg) if products else 0
        per_language = products + shop_pages + categories + posts + blogs + (1 if posts else 0) + builder
        languages = len(website.language_ids) or 1
        rows.append({
            "website": website.display_name,
            "languages": languages,
            "products": products,
            "categories": categories,
            "posts": posts,
            "builder": builder,
            "pages_per_language": per_language,
            "pages": per_language * languages,
        })
    return rows


def estimate(env, dbname, ram_budget_bytes):
    """Everything the dashboard and the automatic backend choice need."""
    rows = count_pages(env)
    pages = sum(r["pages"] for r in rows)
    measured = store.average_size("ram", dbname) or store.average_size("file", dbname)
    page_bytes = measured or DEFAULT_PAGE_BYTES
    need = int(pages * page_bytes)
    mem = meminfo()
    tmpfs = tmpfs_info()
    limits = [ram_budget_bytes]
    if tmpfs.get("size"):
        limits.append(tmpfs["size"])
    if mem.get("available"):
        limits.append(mem["available"] // 2)
    limit = min(limits)
    wanted = int(need / SAFETY)  # room needed to say "fits"
    available = mem.get("available", 0)
    shortfall = {
        "budget": max(0, wanted - ram_budget_bytes),
        "tmpfs": max(0, wanted - tmpfs["size"]) if tmpfs.get("size") else 0,
        # physical memory: the cache may use at most half of what is available
        "memory": max(0, 2 * wanted - available) if available else 0,
    }
    if not store.ram_available():
        verdict = "unavailable"
    elif need <= limit * SAFETY:
        verdict = "fits"
    elif need <= limit:
        verdict = "tight"
    else:
        verdict = "too_big"
    return {
        "rows": rows, "pages": pages, "page_bytes": page_bytes, "measured": bool(measured), "need": need,
        "limit": limit, "budget": ram_budget_bytes, "wanted": wanted, "shortfall": shortfall, "mem": mem, "tmpfs": tmpfs, "verdict": verdict,
    }
