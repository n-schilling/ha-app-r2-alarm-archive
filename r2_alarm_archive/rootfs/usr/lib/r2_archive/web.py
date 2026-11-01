"""A small read-only web page behind Home Assistant's ingress: status and free
tier details, the archived days with their log and snapshots, the queue.

The pages are rendered on the server, without scripts. Everything shown
from the bucket goes through the app; objects never change, so what was read
once is kept in a cache in /data. Every read from R2 passes the free tier
guard; at the limit the pages show what the cache has.
"""

from __future__ import annotations

import asyncio
import calendar
import gzip
import html
import json
import logging
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import archive as a
from .guard import FREE_CLASS_A, FREE_CLASS_B, FREE_STORAGE, LIMITS, utc_month
from .r2 import R2Error
from .store import write_atomic

LOGGER = logging.getLogger(__name__)

PORT = 8099              # the Supervisor's default ingress port
# The Supervisor's ingress proxy; nothing else may talk to the page
INGRESS_PEER = "172.30.32.2"
CACHE_BYTES = 200 * 10**6
CONTENT_TYPES = {".json": "application/json", ".jpg": "image/jpeg", ".gz": "application/gzip",
                 ".txt": "text/plain; charset=utf-8"}

TEXT = {
    "en": {
        "title": "Alarm archive", "overview": "Overview", "days": "Days", "queue": "Queue",
        "status": "Status", "working": "Archiving works", "broken": "Archiving does not work",
        "stopped": "The app does not run", "r2": "R2 connection", "reachable": "reachable",
        "unreachable": "not reachable", "unknown": "not used since the start", "waiting": "Waiting objects",
        "last_upload": "Last upload", "last_summary": "Last summary", "free_tier": "Free tier (whole account)",
        "account": "Account", "bucket": "This bucket", "free": "Free tier",
        "guard_limit": "Guard stops at", "month_end": "End of month (projected)", "storage": "Storage",
        "class_a": "Writes (Class A)", "class_b": "Reads (Class B)", "growth": "Storage growth",
        "per_day": "per day", "days_left": "Guard limit reached in", "days_unit": "days", "never": "not in sight",
        "no_figure": "No usage figure from Cloudflare yet.", "guard_off": "The free tier guard is off.",
        "rules": "Bucket rules", "rules_ok": "Lifecycle and lock rules match the options.",
        "rules_unknown": "Not checked (no api_token).", "day": "Day", "entries": "Entries",
        "snapshots": "Snapshots", "summary": "Summary", "summaries": "Summaries", "none": "none",
        "pictures": "Pictures", "states": "States", "entity": "Entity", "state": "State",
        "changed": "Changed", "from_to": "Change", "reconstructed": "rebuilt from the recorder",
        "missing": "Missing pictures", "download": "Download", "empty_queue": "Nothing waits.",
        "object": "Object", "kind": "Kind", "size": "Size", "age": "Waiting since",
        "cached_only": "The free tier limit is reached: only what was read before is shown.",
        "not_cached": "(not in the cache)", "user": "User", "no_days": "Nothing archived yet.",
        "problems": "Problems",
    },
    "de": {
        "title": "Alarm-Archiv", "overview": "Übersicht", "days": "Tage", "queue": "Warteschlange",
        "status": "Status", "working": "Archivierung läuft", "broken": "Archivierung gestört",
        "stopped": "Die App läuft nicht", "r2": "R2-Verbindung", "reachable": "erreichbar",
        "unreachable": "nicht erreichbar", "unknown": "seit dem Start noch nicht benutzt", "waiting": "Wartende Objekte",
        "last_upload": "Letzter Upload", "last_summary": "Letzte Zusammenfassung",
        "free_tier": "Free-Tier (ganzes Konto)", "account": "Konto", "bucket": "Dieser Bucket",
        "free": "Free-Tier", "guard_limit": "Wächter stoppt bei", "month_end": "Monatsende (hochgerechnet)",
        "storage": "Speicher", "class_a": "Schreiben (Class A)", "class_b": "Lesen (Class B)",
        "growth": "Speicherzuwachs", "per_day": "pro Tag", "days_left": "Wächter-Grenze erreicht in",
        "days_unit": "Tagen",
        "never": "nicht absehbar", "no_figure": "Noch keine Verbrauchszahl von Cloudflare.",
        "guard_off": "Der Free-Tier-Wächter ist aus.", "rules": "Bucket-Regeln",
        "rules_ok": "Lifecycle- und Lock-Regeln passen zu den Optionen.",
        "rules_unknown": "Nicht geprüft (kein api_token).", "day": "Tag", "entries": "Einträge",
        "snapshots": "Snapshots", "summary": "Zusammenfassung", "summaries": "Zusammenfassungen",
        "none": "keine", "pictures": "Bilder", "states": "Zustände", "entity": "Entität",
        "state": "Zustand", "changed": "Geändert", "from_to": "Wechsel",
        "reconstructed": "aus dem Recorder nachgebaut", "missing": "Fehlende Bilder",
        "download": "Herunterladen", "empty_queue": "Nichts wartet.", "object": "Objekt", "kind": "Art",
        "size": "Größe", "age": "Wartet seit",
        "cached_only": "Die Free-Tier-Grenze ist erreicht: Es wird nur angezeigt, was schon gelesen wurde.",
        "not_cached": "(nicht im Zwischenspeicher)", "user": "Benutzer",
        "no_days": "Noch nichts archiviert.", "problems": "Probleme",
    },
}

PANEL_STATES = {
    "en": {"disarmed": "disarmed", "armed_home": "armed home", "armed_away": "armed away",
           "armed_night": "armed night", "armed_vacation": "armed vacation",
           "armed_custom_bypass": "armed custom bypass", "arming": "arming", "pending": "pending",
           "triggered": "ALARM"},
    "de": {"disarmed": "Unscharf", "armed_home": "Zuhause scharf", "armed_away": "Abwesend scharf",
           "armed_night": "Nacht scharf", "armed_vacation": "Urlaub scharf",
           "armed_custom_bypass": "Benutzerdefiniert scharf", "arming": "Wird scharf",
           "pending": "Eingangsverzögerung", "triggered": "ALARM"},
}

# The app's problem texts (see Archive.health) in German
PROBLEMS_DE = {
    "Home Assistant not connected": "App ohne Verbindung zu Home Assistant",
    "R2 not reachable": "Cloudflare R2 nicht erreichbar",
    "free tier limit reached": "Free-Tier-Grenze erreicht",
    "no R2 usage figure for the free tier guard": "Keine Verbrauchszahl von Cloudflare, Uploads angehalten",
}

STYLE = """
:root { color-scheme: light dark; --fg: #1c1c1c; --muted: #6b6b6b; --bg: #fafafa; --card: #fff;
  --line: #e0e0e0; --ok: #2e7d32; --bad: #c77700; --link: #03a9f4; }
@media (prefers-color-scheme: dark) { :root { --fg: #e1e1e1; --muted: #9b9b9b; --bg: #111;
  --card: #1c1c1c; --line: #333; --ok: #66bb6a; --bad: #ffa726; } }
body { font: 15px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; color: var(--fg);
  background: var(--bg); }
nav { display: flex; gap: 18px; padding: 12px 16px; border-bottom: 1px solid var(--line);
  background: var(--card); }
nav a { color: var(--fg); text-decoration: none; } nav a.on { font-weight: 600; }
main { padding: 16px; max-width: 1000px; }
a { color: var(--link); }
section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px;
  margin-bottom: 16px; }
h1 { font-size: 20px; margin: 0 0 12px; } h2 { font-size: 16px; margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; } th, td { text-align: left; padding: 4px 8px;
  border-bottom: 1px solid var(--line); vertical-align: top; } th { color: var(--muted); font-weight: 500; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
.ok { color: var(--ok); } .bad { color: var(--bad); } .muted { color: var(--muted); }
.pics { display: flex; flex-wrap: wrap; gap: 12px; } .pics figure { margin: 0; flex: 1 1 400px; }
.pics img { width: 100%; border-radius: 8px; } td.t { white-space: nowrap; }
pre { white-space: pre-wrap; margin: 0; }
"""


def size_text(n: float, lang: str = "en") -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            text = f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            return text.replace(".", ",") if lang == "de" else text
        n /= 1000
    return f"{n:.1f} GB"


def percent(n: float, lang: str) -> str:
    text = f"{n:.2f} %"
    return text.replace(".", ",") if lang == "de" else text


def number(n: float, lang: str) -> str:
    text = f"{n:,.0f}"
    return text.replace(",", ".") if lang == "de" else text


def problem_text(p: str, lang: str) -> str:
    if lang != "de":
        return p
    if p in PROBLEMS_DE:
        return PROBLEMS_DE[p]
    if p.startswith("last upload failed: "):
        return "Letzter Upload fehlgeschlagen: " + p[20:]
    if " objects waiting, the oldest for " in p:
        words = p.split(" ")
        return f"{words[0]} Objekte warten, das älteste seit {words[-2]} min"
    if p.startswith("summary of ") and p.endswith(" missing"):
        return f"Tageszusammenfassung vom {p[11:-8]} fehlt"
    return p


class Cache:
    """Objects read from R2, as files named after their key; the oldest go
    when the cache grows beyond its size."""

    def __init__(self, folder: Path, max_bytes: int = CACHE_BYTES) -> None:
        self._folder = folder
        self._max = max_bytes
        folder.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._folder / quote(key, safe="")

    def get(self, key: str) -> bytes | None:
        try:
            data = self._path(key).read_bytes()
        except OSError:
            return None
        os.utime(self._path(key))
        return data

    def put(self, key: str, data: bytes) -> None:
        write_atomic(self._path(key), data)
        files = sorted((p for p in self._folder.iterdir() if not p.name.startswith(".")),
                       key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for old in files:
            if total <= self._max:
                break
            total -= old.stat().st_size
            old.unlink(missing_ok=True)


class Blocked(Exception):
    """Reading from R2 would pass the free tier limit."""


class Web:
    def __init__(self, archive, cache: Cache, allowed_peers: set[str] | None = None) -> None:
        self.archive = archive
        self.cache = cache
        self.allowed = {INGRESS_PEER} if allowed_peers is None else allowed_peers

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._only_ingress])
        app.router.add_get("/", self.overview)
        app.router.add_get("/days", self.days)
        app.router.add_get("/day/{day}", self.day)
        app.router.add_get("/snapshot/{day}/{name}", self.snapshot)
        app.router.add_get("/queue", self.queue)
        app.router.add_get("/file/{key:.+}", self.file)
        return app

    async def run(self, port: int = PORT) -> None:
        runner = web.AppRunner(self.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        LOGGER.info("Web page on port %d (ingress)", port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    @web.middleware
    async def _only_ingress(self, request: web.Request, handler):
        if self.allowed and request.remote not in self.allowed:
            raise web.HTTPForbidden(text="only through Home Assistant")
        return await handler(request)

    # --- Helpers -------------------------------------------------------------------
    def t(self, key: str) -> str:
        return TEXT[self.lang][key]

    @property
    def lang(self) -> str:
        return "de" if self.archive.language.lower().startswith("de") else "en"

    @property
    def tz(self):
        return self.archive.tz

    def when(self, value) -> str:
        if not value:
            return "–"
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        local = moment.astimezone(self.tz) if self.tz else moment
        return local.strftime("%d.%m.%Y %H:%M:%S" if self.lang == "de" else "%Y-%m-%d %H:%M:%S")

    def panel_state(self, state) -> str:
        return PANEL_STATES[self.lang].get(str(state), str(state))

    async def user_name(self, user_id: str | None) -> str:
        """The person of a Home Assistant user, else the ID."""
        if not user_id:
            return "–"
        try:
            for state in await self.archive.ha.states():
                if state["entity_id"].startswith("person.") and \
                        (state.get("attributes") or {}).get("user_id") == user_id:
                    return str(state["attributes"].get("friendly_name") or state["entity_id"])
        except Exception as err:  # the page still shows the ID
            LOGGER.debug("No person for %s: %s", user_id, err)
        return user_id

    def day_text(self, day: str) -> str:
        d = date.fromisoformat(day)
        return d.strftime("%d.%m.%Y") if self.lang == "de" else day

    def page(self, request: web.Request, active: str, body: str) -> web.Response:
        base = request.headers.get("X-Ingress-Path", "")
        links = "".join(f'<a href="{base}{href}" class="{"on" if name == active else ""}">{self.t(name)}</a>'
                        for name, href in (("overview", "/"), ("days", "/days"), ("queue", "/queue")))
        body = body.replace("{base}", base)
        return web.Response(content_type="text/html", charset="utf-8", text=(
            f'<!doctype html><html lang="{self.lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{self.t('title')}</title><style>{STYLE}</style></head><body>"
            f"<nav><strong>{self.t('title')}</strong>{links}</nav><main>{body}</main></body></html>"))

    async def read(self, key: str) -> bytes:
        """An object from the cache, or from R2 past the guard."""
        if (data := self.cache.get(key)) is not None:
            return data
        archive = self.archive
        loop_now = asyncio.get_running_loop().time()
        if not archive.guard.allows(loop_now, utc_month(archive.now()), class_b=1):
            raise Blocked(key)
        data = await archive.r2.get(key)
        archive.guard.count(class_b=1)
        self.cache.put(key, data)
        return data

    def day_keys(self, day: str) -> tuple[list[str], list[str]]:
        """(log keys, snapshot folders) of a day from the local index."""
        keys = self.archive.state.data["uploaded"].get(day, [])
        logs = sorted(k for k in keys if k.startswith(a.PREFIX["log"]))
        folders = sorted({k.rsplit("/", 1)[0] for k in keys if k.startswith(a.PREFIX["snapshots"])})
        return logs, folders

    # --- Pages -----------------------------------------------------------------------
    async def overview(self, request: web.Request) -> web.Response:
        archive = self.archive
        problems = archive.health()
        status = archive.status()
        state = (f'<p class="ok">✔ {self.t("working")}</p>' if not problems else
                 f'<p class="bad">⚠ {self.t("broken")}</p><ul>'
                 + "".join(f"<li>{html.escape(problem_text(p, self.lang))}</li>" for p in problems) + "</ul>")
        r2 = {True: self.t("reachable"), False: self.t("unreachable"), None: self.t("unknown")}
        body = (f"<h1>{self.t('overview')}</h1><section><h2>{self.t('status')}</h2>{state}<table>"
                f"<tr><th>{self.t('r2')}</th><td>{r2[archive.r2_connected]}</td></tr>"
                f"<tr><th>{self.t('waiting')}</th><td>{len(archive.queue)}</td></tr>"
                f"<tr><th>{self.t('last_upload')}</th><td>{self.when(status['last_upload'])}</td></tr>"
                f"<tr><th>{self.t('last_summary')}</th><td>{self.when(status['last_summary'])}</td></tr>"
                "</table></section>")
        body += self.free_tier_section()
        if archive.rule_problems is None:
            rules = f'<p class="muted">{self.t("rules_unknown")}</p>'
        elif not archive.rule_problems:
            rules = f'<p class="ok">✔ {self.t("rules_ok")}</p>'
        else:
            commands = a.rule_commands(archive.cfg.bucket, archive.cfg.retention, archive.cfg.lock,
                                       archive.cfg.jurisdiction)
            rules = ("<ul>" + "".join(f'<li class="bad">{html.escape(p)}</li>' for p in archive.rule_problems)
                     + "</ul><pre>" + html.escape("\n".join(commands)) + "</pre>")
        body += f"<section><h2>{self.t('rules')}</h2>{rules}</section>"
        return self.page(request, "overview", body)

    def free_tier_section(self) -> str:
        archive = self.archive
        guard = archive.guard
        notes = "" if guard.enabled else f'<p class="muted">{self.t("guard_off")}</p>'
        totals = guard.totals()
        if totals is None:
            return f"<section><h2>{self.t('free_tier')}</h2>{notes}<p class=\"muted\">{self.t('no_figure')}</p></section>"
        usage = guard.usage
        now = archive.now().astimezone(UTC)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        elapsed = max((now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds()
                      / (days_in_month * 86400), 1 / (days_in_month * 24))
        lang = self.lang
        rows = []
        for name, key, free, own in (("class_a", "class_a", FREE_CLASS_A, usage.bucket_class_a),
                                     ("class_b", "class_b", FREE_CLASS_B, usage.bucket_class_b)):
            value = totals[key]
            rows.append(f"<tr><th>{self.t(name)}</th><td class='n'>{number(value, lang)}</td>"
                        f"<td class='n'>{number(own, lang)}</td><td class='n'>{number(free, lang)}</td>"
                        f"<td class='n'>{percent(100 * value / free, lang)}</td><td class='n'>{number(LIMITS[key], lang)}</td>"
                        f"<td class='n'>{number(value / elapsed, lang)}</td></tr>")
        storage = totals["storage"]
        rows.append(f"<tr><th>{self.t('storage')}</th><td class='n'>{size_text(storage, lang)}</td>"
                    f"<td class='n'>{size_text(usage.bucket_storage_bytes, lang)}</td>"
                    f"<td class='n'>{size_text(FREE_STORAGE, lang)}</td><td class='n'>{percent(100 * storage / FREE_STORAGE, lang)}</td>"
                    f"<td class='n'>{size_text(LIMITS['storage'], lang)}</td><td class='n'>–</td></tr>")
        growth = self.storage_growth()
        if growth is None:
            outlook = ""
        else:
            left = (LIMITS["storage"] - storage) / growth if growth > 0 else None
            outlook = (f"<p>{self.t('growth')}: {size_text(max(growth, 0), self.lang)} {self.t('per_day')} · "
                       f"{self.t('days_left')}: "
                       + (f"{left:.0f} {self.t('days_unit')}" if left is not None and left < 3650 else self.t("never"))
                       + "</p>")
        return (f"<section><h2>{self.t('free_tier')}</h2>{notes}<table><tr><th></th>"
                f"<th class='n'>{self.t('account')}</th><th class='n'>{self.t('bucket')}</th>"
                f"<th class='n'>{self.t('free')}</th><th class='n'>%</th><th class='n'>{self.t('guard_limit')}</th>"
                f"<th class='n'>{self.t('month_end')}</th></tr>{''.join(rows)}</table>{outlook}</section>")

    def storage_growth(self) -> float | None:
        """Bytes per day of the whole account over the last week of samples."""
        days = sorted(self.archive.state.data["storage_days"].items())[-8:]
        if len(days) < 2:
            return None
        (first, (start, _)), (last, (end, _)) = days[0], days[-1]
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days
        return (end - start) / span if span else None

    async def days(self, request: web.Request) -> web.Response:
        data = self.archive.state.data
        all_days = sorted(set(data["uploaded"]) | set(data["summaries"]), reverse=True)
        if not all_days:
            return self.page(request, "days", f"<h1>{self.t('days')}</h1><p>{self.t('no_days')}</p>")
        rows = []
        for day in all_days:
            logs, folders = self.day_keys(day)
            files = (data["summaries"].get(day) or {}).get("files", 0)
            rows.append(f'<tr><td><a href="{{base}}/day/{day}">{self.day_text(day)}</a></td>'
                        f"<td class='n'>{len(logs)}</td><td class='n'>{len(folders)}</td>"
                        f"<td class='n'>{files or '–'}</td></tr>")
        body = (f"<h1>{self.t('days')}</h1><section><table><tr><th>{self.t('day')}</th>"
                f"<th class='n'>{self.t('entries')}</th><th class='n'>{self.t('snapshots')}</th>"
                f"<th class='n'>{self.t('summaries')}</th></tr>{''.join(rows)}</table></section>")
        return self.page(request, "days", body)

    async def day(self, request: web.Request) -> web.Response:
        day = request.match_info["day"]
        try:
            date.fromisoformat(day)
        except ValueError as err:
            raise web.HTTPNotFound() from err
        logs, folders = self.day_keys(day)
        blocked = False
        lines = []
        for key in logs:
            try:
                entry = json.loads(await self.read(key))
                lines.append((entry.get("time", ""), f"<tr><td class='t'>{self.when(entry.get('time'))}</td>"
                              f"<td>{html.escape(str(entry.get('message', '')))}</td></tr>"))
            except Blocked:
                blocked = True
                lines.append(("", f"<tr><td colspan=2 class='muted'>{html.escape(key)} {self.t('not_cached')}</td></tr>"))
            except (R2Error, ValueError) as err:
                lines.append(("", f"<tr><td colspan=2 class='bad'>{html.escape(str(err))}</td></tr>"))
        table = "".join(row for _, row in sorted(lines)) or f"<tr><td class='muted'>{self.t('none')}</td></tr>"
        snaps = "".join(
            f'<li><a href="{{base}}/snapshot/{day}/{quote(f.rsplit("/", 1)[1])}">'
            f"{self.when(self.folder_time(f))} · {html.escape(self.panel_state(f.rsplit('/', 1)[1].split('_', 1)[1]))}</a></li>" for f in folders
        ) or f"<li class='muted'>{self.t('none')}</li>"
        files = (self.archive.state.data["summaries"].get(day) or {}).get("files", 0)
        summaries = "".join(
            f'<li><a href="{{base}}/file/{a.summary_key(day, part)}">{html.escape(a.summary_key(day, part).rsplit("/", 1)[1])}</a> · '
            f'<a href="{{base}}/file/{a.summary_key(day, part)}?download=1">{self.t("download")}</a></li>'
            for part in range(files)) or f"<li class='muted'>{self.t('none')}</li>"
        note = f'<p class="bad">{self.t("cached_only")}</p>' if blocked else ""
        body = (f"<h1>{self.day_text(day)}</h1>{note}<section><h2>{self.t('entries')}</h2><table>{table}</table>"
                f"</section><section><h2>{self.t('snapshots')}</h2><ul>{snaps}</ul></section>"
                f"<section><h2>{self.t('summary')}</h2><ul>{summaries}</ul></section>")
        return self.page(request, "days", body)

    @staticmethod
    def folder_time(folder: str) -> datetime | None:
        stamp = folder.rsplit("/", 1)[1].split("_", 1)[0]
        try:
            return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            return None

    async def snapshot(self, request: web.Request) -> web.Response:
        day, name = request.match_info["day"], request.match_info["name"]
        folder = f"{a.PREFIX['snapshots']}{a.day_folder(day)}{name}"
        keys = [k for k in self.archive.state.data["uploaded"].get(day, []) if k.startswith(folder + "/")]
        if not keys:
            raise web.HTTPNotFound()
        try:
            meta = json.loads(await self.read(folder + "/meta.json"))
            states = json.loads(gzip.decompress(await self.read(folder + "/zustand.json.gz")))
        except Blocked:
            return self.page(request, "days", f'<h1>{html.escape(name)}</h1><p class="bad">{self.t("cached_only")}</p>')
        rows = "".join(
            f"<tr><td>{html.escape(str((s.get('attributes') or {}).get('friendly_name') or s['entity_id']))}"
            f"<br><span class='muted'>{html.escape(s['entity_id'])}</span></td>"
            f"<td>{html.escape(str(s.get('state')))}</td><td>{self.when(s.get('last_changed'))}</td></tr>"
            for s in states.get("states", []))
        pictures = "".join(
            f'<figure><img src="{{base}}/file/{folder}/{quote(file)}" alt="{html.escape(cam)}">'
            f'<figcaption>{html.escape(cam)} · <a href="{{base}}/file/{folder}/{quote(file)}?download=1">'
            f"{self.t('download')}</a></figcaption></figure>"
            for cam, file in (meta.get("cameras") or {}).items())
        missing = meta.get("missing_cameras") or []
        info = (f"<table><tr><th>{self.t('from_to')}</th><td>{html.escape(self.panel_state(meta.get('from')))} → "
                f"{html.escape(self.panel_state(meta.get('to')))}</td></tr>"
                f"<tr><th>{self.t('user')}</th><td>{html.escape(await self.user_name(meta.get('user_id')))}</td></tr>"
                + (f"<tr><th>{self.t('missing')}</th><td>{html.escape(', '.join(missing))}</td></tr>" if missing else "")
                + "</table>" + (f"<p class='muted'>{self.t('reconstructed')}</p>" if meta.get("reconstructed") else ""))
        downloads = " · ".join(f'<a href="{{base}}/file/{folder}/{f}?download=1">{f}</a>'
                               for f in ("meta.json", "zustand.json.gz"))
        body = (f"<h1>{self.when(meta.get('time'))} · {html.escape(self.panel_state(meta.get('to')))}</h1>"
                f"<section>{info}<p>{self.t('download')}: {downloads}</p></section>"
                + (f"<section><h2>{self.t('pictures')}</h2><div class='pics'>{pictures}</div></section>" if pictures else "")
                + f"<section><h2>{self.t('states')}</h2><table><tr><th>{self.t('entity')}</th>"
                f"<th>{self.t('state')}</th><th>{self.t('changed')}</th></tr>{rows}</table></section>")
        return self.page(request, "days", body)

    async def queue(self, request: web.Request) -> web.Response:
        items = self.archive.queue.items()
        if not items:
            return self.page(request, "queue", f"<h1>{self.t('queue')}</h1><p>{self.t('empty_queue')}</p>")
        now = time.time()
        rows = "".join(
            f"<tr><td>{html.escape(i.key)}</td><td>{html.escape(i.kind)}</td>"
            f"<td class='n'>{size_text(i.size, self.lang)}</td>"
            f"<td>{self.when(datetime.fromtimestamp(i.queued, UTC))} ({int((now - i.queued) // 60)} min)</td></tr>"
            for i in items)
        body = (f"<h1>{self.t('queue')}</h1><section><table><tr><th>{self.t('object')}</th><th>{self.t('kind')}</th>"
                f"<th class='n'>{self.t('size')}</th><th>{self.t('age')}</th></tr>{rows}</table></section>")
        return self.page(request, "queue", body)

    async def file(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if ".." in key or not key.startswith(tuple(a.PREFIX.values())):
            raise web.HTTPNotFound()
        try:
            data = await self.read(key)
        except Blocked as err:
            raise web.HTTPServiceUnavailable(text=self.t("cached_only")) from err
        except R2Error as err:
            raise web.HTTPNotFound(text=str(err)) from err
        suffix = Path(key).suffix
        headers = {"Cache-Control": "private, max-age=86400"}
        if request.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="{Path(key).name}"'
        return web.Response(body=data, content_type=CONTENT_TYPES.get(suffix, "application/octet-stream").split(";")[0],
                            charset="utf-8" if suffix == ".txt" else None, headers=headers)
