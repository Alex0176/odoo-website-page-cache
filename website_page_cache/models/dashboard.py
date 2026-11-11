# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models

from .. import changes, fit, settings, store, triggers

PARAM = settings.PREFIX


def human_bytes(number):
    number = float(number or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(number) < 1024 or unit == "GB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024


class PageCacheDashboard(models.TransientModel):
    _name = "website.page.cache.dashboard"
    _description = "Page cache dashboard"

    enabled = fields.Boolean(
        help="Serve public shop and blog pages from the cache. Switching it off sends every visitor to "
        "the normal rendering at once.")
    pages = fields.Boolean(
        string="Cache website pages",
        help="Also serve the pages made with the website builder (home page, contact page, ...) from the cache.")
    exclude = fields.Text(
        string="Never cache",
        help="One path per line, * as wildcard, without language prefix, e.g. /contactus or /event/*. "
        "Lines starting with # are ignored.")
    backend = fields.Selection(
        [("auto", "Automatic"), ("file", "Files on disk"), ("ram", "RAM (shared, tmpfs)")],
        help="Automatic uses RAM while all pages fit into the RAM budget, otherwise files. "
        "RAM is a tmpfs shared by all workers, so pages are held once, not once per worker.")
    ttl_hours = fields.Float(
        string="Maximum age (hours)", help="Safety net only: pages are also removed as soon as something they show changes.")
    max_mb_file = fields.Integer(
        string="Disk limit (MB)")
    max_mb_ram = fields.Integer(
        string="RAM budget (MB)", help="The most memory the RAM cache may use. Also the limit the \"fits into RAM\" verdict is measured against.")

    backend_in_use = fields.Char(compute="_compute_overview")
    entries = fields.Char(string="Cached pages", compute="_compute_overview")
    journal = fields.Char(string="Change tracking", compute="_compute_overview")
    journal_ok = fields.Boolean(compute="_compute_overview")
    stats_html = fields.Html(string="Statistics", compute="_compute_stats_html", sanitize=False)
    fit_html = fields.Html(string="Does it fit into RAM?", compute="_compute_fit_html", sanitize=False)

    @api.model
    def action_open(self):
        record = self.create({})
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Page cache"),
            "res_model": self._name,
            "res_id": record.id,
            "view_mode": "form",
            "target": "current",
        }

    # settings -----------------------------------------------------------------------------------

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = self.env._("Page cache")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        params = settings.read_params(self.env.cr)
        res.update({
            "enabled": params["enabled"].strip().lower() not in ("0", "false", "no", ""),
            "pages": params["pages"].strip().lower() not in ("0", "false", "no", ""),
            "exclude": params["exclude"],
            "backend": params["backend"] if params["backend"] in ("auto", "file", "ram") else "auto",
            "ttl_hours": settings._number(params["ttl_hours"], 12),
            "max_mb_file": int(settings._number(params["max_mb_file"], 1024)),
            "max_mb_ram": int(settings._number(params["max_mb_ram"], 256)),
        })
        return {k: v for k, v in res.items() if k in fields_list or not fields_list}

    def write(self, vals):
        result = super().write(vals)
        params = self.env["ir.config_parameter"].sudo()
        rec = self[:1]
        pushed = False
        for name, value in (
            ("enabled", lambda: "1" if rec.enabled else "0"),
            ("pages", lambda: "1" if rec.pages else "0"),
            ("exclude", lambda: rec.exclude or ""),
            ("backend", lambda: rec.backend or "auto"),
            ("ttl_hours", lambda: str(max(rec.ttl_hours, 0.1))),
            ("max_mb_file", lambda: str(max(rec.max_mb_file, 1))),
            ("max_mb_ram", lambda: str(max(rec.max_mb_ram, 1))),
        ):
            if name in vals:
                params.set_param(PARAM + name, value())
                pushed = True
        if pushed:
            settings.reset(self.env.cr.dbname)
        return result

    # overview -----------------------------------------------------------------------------------

    def _compute_overview(self):
        env = self.env
        dbname = env.cr.dbname
        st = settings.get(env.cr, dbname)
        count, size = store.usage(st["backend"], dbname)
        missing = triggers.missing_tables(env.cr)
        env.cr.execute("SELECT to_regclass(%s)", [triggers.LOG_TABLE])
        has_log = env.cr.fetchone()[0] is not None
        rows = 0
        if has_log:
            env.cr.execute(
                f"SELECT count(*) FROM {triggers.LOG_TABLE} "
                "WHERE create_date > (now() AT TIME ZONE 'utc') - interval '1 day'")
            rows = env.cr.fetchone()[0]
        installed = len(triggers.SPECS) - len(missing)
        for rec in self:
            label = dict(self._fields["backend"].selection)[st["backend"]]
            rec.backend_in_use = env._("%(backend)s (limit %(limit)s)", backend=label, limit=human_bytes(st["max_bytes"]))
            rec.entries = env._("%(count)s pages, %(size)s", count=count, size=human_bytes(size))
            rec.journal_ok = has_log and not missing
            rec.journal = env._("%(rows)s changes in the last 24 hours, watching %(installed)s tables",
                                rows=rows, installed=installed) if rec.journal_ok else env._(
                "INCOMPLETE - %(missing)s tables are not watched, the cache must not be trusted",
                missing=len(missing) if has_log else len(triggers.SPECS))

    # statistics ---------------------------------------------------------------------------------

    def _window(self, days):
        stat = self.env["website.page.cache.stat"].sudo()
        since = fields.Date.today() - timedelta(days=days - 1)
        result = stat._read_group(
            [("date", ">=", since)], aggregates=[
                "hits:sum", "misses:sum", "stale:sum", "hit_time:sum", "miss_time:sum", "bytes_served:sum"])
        return [v or 0 for v in result[0]] if result else [0] * 6

    SAVING_FIELDS = ("hits_sized", "hits_unsized", "bytes_original", "bytes_minified", "bytes_sent",
                     "asset_hits", "asset_original", "asset_sent")

    def _saving_window(self, days):
        """``{website name: {field: sum}}`` plus the key ``None`` for the total, for the last ``days`` days."""
        stat = self.env["website.page.cache.stat"].sudo()
        since = fields.Date.today() - timedelta(days=days - 1)
        rows = stat._read_group(
            [("date", ">=", since)], groupby=["website_id"], aggregates=[f"{f}:sum" for f in self.SAVING_FIELDS])
        result = {None: dict.fromkeys(self.SAVING_FIELDS, 0)}
        for website, *values in rows:
            line = {f: v or 0 for f, v in zip(self.SAVING_FIELDS, values)}
            result[website.display_name] = line
            for name, value in line.items():
                result[None][name] += value
        return result

    @staticmethod
    def _percent(part, whole):
        return f"{100 * part / whole:.1f} %" if whole else "-"

    def _unsized_entries(self):
        """``(entries without size data, entries inspected)`` among the newest cached pages."""
        st = settings.get(self.env.cr, self.env.cr.dbname)
        newest = sorted(store.entries(st["backend"], self.env.cr.dbname), key=lambda e: e[2], reverse=True)[:1000]
        unsized = 0
        for path, _size, _mtime in newest:
            meta = store.read_meta(path) or {}
            if not meta.get("size_original"):
                unsized += 1
        return unsized, len(newest)

    def _saving_html(self):
        env = self.env
        head = "".join(f"<th class='text-end'>{escape(h)}</th>" if i > 1 else f"<th>{escape(h)}</th>" for i, h in enumerate((
            env._("Period"), env._("Website"), env._("Pages measured"), env._("Original (without optimisation)"),
            env._("After minify"), env._("Sent (compressed)"), env._("Saved"), env._("Saved %"),
            env._("Assets: original"), env._("Assets: sent (gzip)"), env._("Assets: saved"))))
        body = ""
        for label, days in ((env._("Today"), 1), (env._("7 days"), 7), (env._("30 days"), 30)):
            window = self._saving_window(days)
            names = [None] + sorted(n for n in window if n)
            for name in names:
                v = window[name]
                if name and not (v["hits_sized"] or v["asset_hits"]):
                    continue
                saved = v["bytes_original"] - v["bytes_sent"]
                asset_saved = v["asset_original"] - v["asset_sent"]
                cells = [label if name is None else "", env._("All websites") if name is None else name,
                         f"{v['hits_sized']:,}", human_bytes(v["bytes_original"]), human_bytes(v["bytes_minified"]),
                         human_bytes(v["bytes_sent"]), human_bytes(saved) if v["hits_sized"] else "-",
                         self._percent(saved, v["bytes_original"]),
                         human_bytes(v["asset_original"]), human_bytes(v["asset_sent"]),
                         f"{human_bytes(asset_saved)} ({self._percent(asset_saved, v['asset_original'])})"
                         if v["asset_hits"] else "-"]
                css = " class='fw-bold'" if name is None else ""
                body += f"<tr{css}>" + "".join(
                    f"<td class='text-end'>{escape(c)}</td>" if i > 1 else f"<td>{escape(c)}</td>"
                    for i, c in enumerate(cells)) + "</tr>"
        total = self._saving_window(30)[None]
        sized = total["hits_sized"]
        average = ""
        if sized:
            per = lambda key: total[key] / sized  # noqa: E731
            average = env._(
                "Average per page (30 days, %(count)s measured hits): original %(orig)s, after minify %(mini)s, "
                "sent %(sent)s - minify saves %(minify)s, gzip a further %(gzip)s (clients without gzip count "
                "with no saving), together %(both)s.",
                count=f"{sized:,}", orig=human_bytes(per("bytes_original")), mini=human_bytes(per("bytes_minified")),
                sent=human_bytes(per("bytes_sent")),
                minify=self._percent(total["bytes_original"] - total["bytes_minified"], total["bytes_original"]),
                gzip=self._percent(total["bytes_minified"] - total["bytes_sent"], total["bytes_minified"]),
                both=self._percent(total["bytes_original"] - total["bytes_sent"], total["bytes_original"]))
        unsized_entries, inspected = self._unsized_entries()
        note = env._(
            "Without data (not estimated): %(hits)s hits of pages stored before the update (30 days), "
            "%(entries)s of %(inspected)s cached pages have no original size yet and are measured once they are "
            "stored again. Assets: only gzip copies made after the update are counted.",
            hits=f"{total['hits_unsized']:,}", entries=unsized_entries, inspected=inspected)
        return Markup(
            "<h5>{title}</h5><table class='table table-sm'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            "<p>{average}</p><p class='text-muted'>{note}</p>").format(
            title=env._("Saved by minify and compression"), head=Markup(head), body=Markup(body),
            average=average, note=note)

    def _compute_stats_html(self):
        env = self.env
        head = "".join(f"<th class='text-end'>{escape(h)}</th>" if i else f"<th>{escape(h)}</th>" for i, h in enumerate((
            env._("Period"), env._("Served from cache"), env._("Rendered"), env._("Hit rate"),
            env._("Removed (changed/expired)"), env._("Render time"), env._("Cache time"),
            env._("Estimated server time saved"), env._("Traffic from cache"))))
        body = ""
        for label, days in ((env._("Today"), 1), (env._("7 days"), 7), (env._("30 days"), 30)):
            hits, misses, stale, hit_time, miss_time, served = self._window(days)
            total = hits + misses
            ratio = f"{100 * hits / total:.1f} %" if total else "-"
            avg_miss = f"{1000 * miss_time / misses:.0f} ms" if misses else "-"
            avg_hit = f"{1000 * hit_time / hits:.1f} ms" if hits else "-"
            saved = hits * (miss_time / misses - hit_time / hits) if hits and misses else 0
            saved_text = self._duration(saved) if saved > 0 else "-"
            cells = [label, f"{hits:,}", f"{misses:,}", ratio, f"{stale:,}", avg_miss, avg_hit, saved_text,
                     human_bytes(served)]
            body += "<tr>" + "".join(
                f"<td class='text-end'>{escape(c)}</td>" if i else f"<td>{escape(c)}</td>"
                for i, c in enumerate(cells)) + "</tr>"
        note = env._("Render time and cache time are averages per page. Numbers are written once a minute.")
        html = Markup(
            "<table class='table table-sm'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            "<p class='text-muted'>{note}</p>{saving}").format(
            head=Markup(head), body=Markup(body), note=note, saving=self._saving_html())
        for rec in self:
            rec.stats_html = html

    @staticmethod
    def _duration(seconds):
        seconds = int(seconds)
        if seconds >= 3600:
            return f"{seconds // 3600} h {seconds % 3600 // 60} min"
        if seconds >= 60:
            return f"{seconds // 60} min {seconds % 60} s"
        return f"{seconds} s"

    # does it fit into RAM -----------------------------------------------------------------------

    def _compute_fit_html(self):
        env = self.env
        params = settings.read_params(env.cr)
        budget = int(settings._number(params["max_mb_ram"], 256) * 1048576)
        result = fit.estimate(env, env.cr.dbname, budget)
        verdicts = {
            "fits": (env._("Yes - the pages fit into RAM with room to spare."), "text-success"),
            "tight": (env._("Barely - the pages fit, but without a safety margin."), "text-warning"),
            "too_big": (env._("No - the pages are larger than the RAM available for the cache. "
                              "Use files or raise the RAM budget."), "text-danger"),
            "unavailable": (env._("RAM cache is not available on this server (no writable /dev/shm)."),
                            "text-danger"),
        }
        text, css = verdicts[result["verdict"]]
        head = "".join(f"<th class='text-end'>{escape(h)}</th>" if i else f"<th>{escape(h)}</th>" for i, h in enumerate((
            env._("Website"), env._("Languages"), env._("Products"), env._("Categories"), env._("Blog posts"),
            env._("Website pages"),
            env._("Pages per language"), env._("Pages"))))
        body = "".join(
            "<tr><td>{}</td>{}</tr>".format(
                escape(row["website"]),
                "".join(f"<td class='text-end'>{row[k]:,}</td>" for k in (
                    "languages", "products", "categories", "posts", "builder", "pages_per_language", "pages")))
            for row in result["rows"])
        mem, tmpfs = result["mem"], result["tmpfs"]
        size_note = (env._("measured average of the cached pages") if result["measured"]
                     else env._("assumed, nothing cached yet"))
        lines = [
            env._("Estimated pages: %(pages)s, about %(size)s each (%(how)s, compressed) = %(need)s",
                  pages=f"{result['pages']:,}", size=human_bytes(result["page_bytes"]), how=size_note,
                  need=human_bytes(result["need"])),
            env._("Room for the cache: %(limit)s = the smallest of: RAM budget %(budget)s, tmpfs size %(tmpfs)s, "
                  "half of the available memory %(avail)s",
                  limit=human_bytes(result["limit"]), budget=human_bytes(result["budget"]),
                  tmpfs=human_bytes(tmpfs.get("size")) if tmpfs else "-",
                  avail=human_bytes(mem.get("available", 0) // 2) if mem else "-"),
        ]
        short = result["shortfall"]
        if short["memory"]:
            lines.append(env._(
                "Additional RAM needed for a RAM cache: about %(extra)s more memory on this server "
                "(%(wanted)s for the cache including a safety margin, and the cache may use at most half "
                "of the available memory, now %(avail)s).",
                extra=human_bytes(short["memory"]), wanted=human_bytes(result["wanted"]),
                avail=human_bytes(mem.get("available", 0))))
        elif result["verdict"] != "unavailable":
            lines.append(env._(
                "Additional RAM needed for a RAM cache: none - %(wanted)s are enough including a safety margin.",
                wanted=human_bytes(result["wanted"])))
        settings_hint = []
        if short["budget"]:
            settings_hint.append(env._("raise the RAM budget to at least %(size)s",
                                       size=human_bytes(result["wanted"])))
        if short["tmpfs"]:
            settings_hint.append(env._("enlarge the tmpfs /dev/shm to at least %(size)s (mount option size=)",
                                       size=human_bytes(result["wanted"])))
        if settings_hint:
            lines.append(env._("Settings to change: %(hints)s.", hints="; ".join(settings_hint)))
        assumption = env._(
            "Counted: published products, shop list pages, categories, blog posts and blog lists and pages of the website builder, per language "
            "of each website. Not counted: pages for other pricelists or countries - add some margin.")
        html = Markup(
            "<p class='fw-bold {css}'>{text}</p>"
            "<table class='table table-sm'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            "<p>{lines}</p><p class='text-muted'>{assumption}</p>"
        ).format(
            css=css, text=text, head=Markup(head), body=Markup(body),
            lines=Markup("<br/>").join(escape(line) for line in lines), assumption=assumption)
        for rec in self:
            rec.fit_html = html

    # buttons ------------------------------------------------------------------------------------

    def _reload(self):
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_clear(self):
        self.env["website.page.cache.service"]._clear()
        return self._reload()

    def action_check_triggers(self):
        self.env["website.page.cache.service"]._ensure_triggers()
        return self._reload()

    def action_maintain(self):
        self.env["website.page.cache.service"]._cron_maintain()
        return self._reload()

    def action_refresh(self):
        return self._reload()
