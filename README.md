# Website Page Cache for Odoo 19

Serves the public **shop, blog and website-builder pages** of an Odoo 19 website from a shared cache instead of
rendering them again for every visitor, crawler and AI indexer. A cached page is delivered in a few
milliseconds; the first request after a change renders it again and caches it anew.

Nothing is blocked or rate-limited: search engines and AI crawlers get the same pages as everyone
else, only faster.

## What is cached

Only requests that are guaranteed to produce the same page for everybody:

* `GET` without query string, anonymous (public) visitor, status 200, `text/html`;
* `/shop`, `/shop/page/N`, `/shop/category/...`, product pages, `/blog`, blog lists and posts - in
  every language of every website;
* pages made with the website builder (home page, contact page, ...) - served by the same cache
  and invalidated by changes to the page, its views, menus, snippets, products and blog posts
  (dynamic snippets). Switch: *Cache website pages*;
* a **"Never cache" list** (one path per line, `*` as wildcard, without language prefix, e.g.
  `/contactus` or `/event/*`) keeps individual pages out of the cache, for example pages with
  per-visitor content such as a captcha;
* the cache key contains website, host, language, path, pricelist, fiscal position and the cookie
  consent state, so visitors who would see something different never share an entry;
* visitors with a cart, a login or any other session state, `?search=...`, filters, checkout and
  forms are never touched.

The CSRF token inside a page is replaced per request, blog post views are still counted, and cached
responses carry `X-Page-Cache: HIT` (rendered and stored: `MISS`) so you can see what happened.

## Storage: files or RAM

Setting *Backend* (Website > Configuration > Reporting > Page cache > Dashboard):

| Value | Where | Notes |
|-------|-------|-------|
| Files on disk | `<data_dir>/page_cache/<db>/` | survives restarts; the OS page cache keeps hot pages in memory |
| RAM | tmpfs under `/dev/shm` | shared by all workers, so every page is held **once** |
| Automatic | RAM while everything fits, otherwise files | decided by the maintenance job; stays on RAM while the verdict is "fits" or "tight" |

The dashboard answers *"do the websites fit into RAM at all?"*: it counts the pages (published
products, shop list pages, categories, blog posts, blog lists, website-builder pages, times the languages of each website),
measures the real size of the cached pages (a default is used until something is cached), and
compares the result with the configured RAM budget, the size of the tmpfs and half of the available
memory. Verdicts: *fits*, *tight*, *too big*, *unavailable*.
It also states how much additional RAM a RAM cache would need on the server (including a safety
margin, and respecting that the cache may use at most half of the available memory) and which
settings to change (RAM budget, tmpfs size).

## Changes remove pages from the cache

Pages are invalidated by what they show, not by a timer. PostgreSQL triggers write every change of
a relevant table (products, variants, images, stock, attributes, categories, prices, taxes,
pricelists, blog posts and tags, partners and company data, views, assets, menus, languages,
website settings ...) into a journal table. A cached page remembers the database snapshot it was
rendered with and the records it shows; a journal entry that is not part of that snapshot and
touches one of those records makes the page stale. The next request throws it away, renders it
again and caches the new version.

* **Translations** are covered: Odoo 19 stores translated terms in the same row (jsonb), so editing
  a translation is a row change like any other.
* Changes made by **raw SQL**, imports, scripts and the ORM are all seen, because the trigger sits
  in the database.
* Changes that affect every page (templates, assets, languages, menus, taxes, currencies, ...)
  invalidate everything at once.
* Blog `visits` counter updates are ignored on purpose, otherwise every view would invalidate the post.
* A maximum age (default 12 h) is only a safety net.

The triggers never make a write fail: an error inside a trigger is logged as a warning.

## Statistics

Hits, misses, stored and invalidated pages, time spent rendering vs. serving from cache, estimated
server time saved and traffic from cache - today, 7 and 30 days on the dashboard, per day and
website as graph/pivot/list under *History*. Counters are collected per process and written once a
minute; rows older than 90 days are removed.

## Install

```
odoo-bin -d <db> -i website_page_cache --stop-after-init
```

then restart Odoo so all workers load the module. The installation creates the journal, the
triggers and a maintenance cron (every 10 minutes). **Uninstalling** removes triggers, journal table
and all cached pages.

Parameters (`ir.config_parameter`, normally edited on the dashboard): `website_page_cache.enabled`,
`.backend` (`auto`/`file`/`ram`), `.ttl_hours`, `.max_mb_file`, `.max_mb_ram`.
Switching the cache off (`enabled = 0`) takes effect in all workers within 30 seconds.

## Limits worth knowing

* After a change, a page can still be served for up to **2 seconds** (the journal index refreshes
  that often).
* Stock-dependent texts that Odoo derives from `stock.move` are refreshed through quant changes and
  the maximum age.
* The size estimate does not count extra variants per pricelist or country; keep some margin.
* A page that contains the visitor's session id is never cached (logged as a warning).
* Several databases per Odoo instance are supported; each has its own entries, journal and statistics.

## License

LGPL-3.0 or later.
