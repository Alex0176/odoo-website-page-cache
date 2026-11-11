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
* the cache key contains website, host class, language, path, pricelist, fiscal position and the cookie
  consent state, so visitors who would see something different never share an entry. The host is
  normalized (see below) so that crawlers and alias hosts do not each get their own copy;
* visitors with a cart, a login or any other session state, `?search=...`, filters, checkout and
  forms are never touched.

The CSRF token inside a page is replaced per request, blog post views are still counted, and cached
responses carry `X-Page-Cache: HIT` (rendered and stored: `MISS`) so you can see what happened.

## Host normalization

Odoo writes every absolute URL (canonical link, `og:url`, hreflang, JSON-LD, sitemap) from the website's
`domain`, not from the `Host` header. The request URL only decides two things in the page: whether it is
the canonical URL (hreflang links) and whether it is indexable (matches the domain ignoring `www.` and
the scheme; otherwise `<meta name="robots" content="noindex">`). The key therefore holds `canonical` for the
website's own domain, `other:1` for indexable but non-canonical URLs (`http://`, bare domain) and `other:0`
for every foreign host: one entry serves all crawler and alias hosts, they keep their `noindex` page, and the
canonical page is never served to a foreign host. Switch: `website_page_cache.normalize_host`
(default `1`, `0` = raw host in the key).

## Images of companies and contacts

`/web/image/res.partner/<id>/image_128`, `avatar_*`, `res.company` logos and similar URLs without a
`unique` hash are answered by Odoo with `Cache-Control: no-cache`, so browsers ask again on every page
view. This module sends `Cache-Control: max-age=2592000` (30 days) for them instead; the ETag stays,
so a changed image shows up latest after that time. URLs with `unique`, `nocache` or `download` are
left alone. System parameter `website_page_cache.image_days` changes the days, `0` turns it off.

## Sitemap

Odoo regenerates `/sitemap.xml` inside the request that finds the stored copy older than 12 hours. With many
posts and languages that takes 20 to 40 seconds and blocks a worker, and each crawler arriving in that
window waits too. The module serves the stored copy while it is younger than
`website_page_cache.sitemap_max_hours` (default 36) and renews it in the background: the hourly cron
"Page cache: renew sitemaps" picks the website whose copy is oldest and past
`website_page_cache.sitemap_refresh_hours` (default 8) and calls `/sitemap.xml` on the local server with a
secret header (parameter `website_page_cache.sitemap_secret`, created by the cron). During that call the
stored copy is back-dated inside the transaction, so Odoo regenerates it while everybody else keeps
reading the old copy until the new one is committed. Each domain a crawler uses has its own stored copy
in Odoo; only the domain set on the website is renewed in the background, the others still fall back to
the core behaviour once their copy is older than the maximum age.

## Minify and compression

**HTML minify** (`website_page_cache.minify`, default `1`): before a page is stored, `minify.py` shrinks the
HTML (typically 10 to 30 %, about 10 % after gzip). The page that is rendered for the first request (`MISS`)
is minified in the same way, so `MISS` and `HIT` are identical. It is deliberately conservative:

* HTML comments go (conditional comments and `<!--!` stay); runs of white space become one space, and white
  space is removed completely only around block elements, table parts and `<br>`, where the browser ignores
  it anyway. A tag that says `inline`, `display`, `float` or `white-space` is not treated as a block, so the gap
  between two inline blocks stays; elements with `white-space: pre*` are not touched;
* `<pre>`, `<textarea>` and `<script>` are not touched (no renaming, no white space removal in inline
  JavaScript); `<script type="application/ld+json">` is re-serialised compactly when it parses, else left as is;
  `<style>` gets a simple CSS minification that respects strings and `url()`;
* anything unexpected (unclosed raw-text element, invalid UTF-8, no gain) returns the original, and an exception
  is logged and the page is delivered as rendered.

Tried on seven real pages of five websites with a browser (JavaScript off): text, element count, the
position of every element and the screenshot were identical to the original. Pages already in the cache are
not minified again; they are replaced when they expire or change.

**Compression** (`website_page_cache.compress`, default `1`): a cache `HIT` is delivered gzip-compressed
(`Content-Encoding: gzip`, `Vary: Accept-Encoding`, correct `Content-Length`) when the client sends
`Accept-Encoding: gzip`; the CSRF token is put in before compressing. The `/web/assets/*.css|js` bundles
(versioned URLs, immutable) are answered by Odoo uncompressed, up to several MB. For gzip clients the module
compresses each bundle once and keeps the copy under `<data_dir>/page_cache/assets_gz/<db>/`; later requests
are served from there (`X-Page-Cache: GZ`, `Cache-Control` and `Content-Type` as in the original). The copies
are removed after 30 days, or oldest first when the folder exceeds 256 MB (maintenance job), and by *clear
cache*. A proxy in front must not compress an answer that already has `Content-Encoding` a second time.

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

### RAM cache and host reboots

tmpfs survives a restart of Odoo but not a reboot of the host. With `website_page_cache.persist` (default `1`)
and the RAM backend, the maintenance job copies new and changed entries (only those) to
`<data_dir>/page_cache_mirror/<db>/` (atomic, at most the RAM budget) and removes entries that are gone from RAM.
When Odoo starts and the RAM directory is empty, the entries are copied back (at most a few seconds at start,
the maintenance job finishes a larger restore; the mirror is not pruned meanwhile). Restored entries are judged like
any other: age and the change journal decide, a page that changed while the host was down is discarded on
first use. Emptying the cache or recreating the journal empties the mirror too. Restored pages keep the
minification they were stored with; they are not recomputed.

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

### What minify and compression save (since 19.0.1.4.0)

A stored page remembers its size before minify (`size_original`) and after (`size_stored`). Each `HIT` adds to
the day/website/process counters: bytes without optimisation, bytes after minify and bytes really sent (after gzip,
or the minified size for clients without gzip). `GZ` hits of `/web/assets` count the uncompressed and the sent
bytes of the bundle. The dashboard shows both per period (today, 7, 30 days) and website: original, after minify,
sent, saved MB and %, plus the average per page (minify %, gzip %). Pages stored before the update have no original
size: their hits are only counted as "without data" and nothing is estimated; they get one when the page is stored
again. The new counter columns are added with `DEFAULT 0`, so old statistics rows stay valid. The bytes are sums over
cache hits only (the first rendering of a page is not counted); the original size is the rendered HTML, which is
what Odoo would have sent without the module.

## Install

```
odoo-bin -d <db> -i website_page_cache --stop-after-init
```

then restart Odoo so all workers load the module. The installation creates the journal, the
triggers and a maintenance cron (every 10 minutes). **Uninstalling** removes triggers, journal table
and all cached pages.

Parameters (`ir.config_parameter`, normally edited on the dashboard): `website_page_cache.enabled`,
`.backend` (`auto`/`file`/`ram`), `.ttl_hours`, `.max_mb_file`, `.max_mb_ram`, `.minify`, `.compress`, `.persist`, `.normalize_host`.
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
