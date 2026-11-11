"""A small read-only web page behind Home Assistant's ingress, laid out like
Home Assistant's settings: a menu, the overview with status and free tier
details, the archived days grouped by month with their log and snapshots,
the queue, and a link to the app's options.

The pages are rendered on the server, each with the same top bar: name,
the time of the request, refresh and the app's options. Their only script is
a small inline one that makes the refresh button reload the page, takes
Home Assistant's theme (colors, card shape, fonts) from the page around the
ingress frame and keeps the viewer's interface language in a cookie
(ha-lang.js), in which the server renders the pages; without it the page
falls back to Home Assistant's default colors and configured language. Everything shown from the bucket goes through the
app; objects never change, so what was read once is kept in a cache in /data.
Every read from R2 passes the free tier guard; at the limit the pages show
what the cache has. Only keys as the app writes them are shown or packed,
and every value is escaped for where it lands; a Content-Security-Policy
allows the one script and nothing else on top.
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import contextlib
import contextvars
import gzip
import hashlib
import html
import json
import logging
import os
import tempfile
import time
import zipfile
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
# The app's name (config.yaml): the start page's title and the version line
APP_NAME = "R2 Alarm Archive"
# Reads from R2 at the same time for a ZIP export
EXPORT_READS = 4
CONTENT_TYPES = {".json": "application/json", ".jpg": "image/jpeg", ".gz": "application/gzip",
                 ".txt": "text/plain; charset=utf-8"}

TEXT = {
    "en": {
        "overview": "Overview", "details": "Details", "queue": "Queue",
        "settings": "Settings", "back": "Back", "refresh": "Refresh", "as_of": "As of {}",
        "overview_info": "Status and free tier usage",
        "details_info": "Log, snapshots and summaries by month",
        "queue_info": "Objects waiting for their upload", "settings_info": "The app's options in Home Assistant",
        "status": "Status", "working": "Archiving works", "broken": "Archiving does not work",
        "r2": "R2 connection", "reachable": "reachable",
        "unreachable": "not reachable", "unknown": "not used since the start", "waiting": "Waiting objects",
        "last_upload": "Last upload", "last_summary": "Last summary", "free_tier": "Free tier (whole account)",
        "account": "Account", "bucket": "This bucket", "free": "Free tier",
        "guard_limit": "Guard stops at", "month_end": "End of month (projected)", "storage": "Storage",
        "class_a": "Writes (Class A)", "class_b": "Reads (Class B)", "growth": "Storage growth",
        "per_day": "per day", "days_left": "Guard limit reached in", "days_unit": "days", "never": "not in sight",
        "no_figure": "No usage figure from Cloudflare yet.", "guard_off": "The free tier guard is off.",
        "day": "Day", "days": "days", "entries": "Entries",
        "log": "Log", "snapshots": "Snapshots", "alarms": "Alarms", "summary": "Summary",
        "summaries": "Summaries", "none": "none", "time": "Time", "message": "Message",
        "pictures": "Pictures", "states": "States", "entity": "Entity", "state": "State",
        "changed": "Changed", "from_to": "Change", "reconstructed": "Rebuilt from the recorder",
        "missing": "Missing pictures", "download": "Download", "files": "Files", "empty_queue": "Nothing waits.",
        "object": "Object", "kind": "Kind", "size": "Size", "age": "Waiting since",
        "cached_only": "The free tier limit is reached: only what was read before is shown.",
        "not_cached": "(not in the cache)", "user": "User", "no_days": "Nothing archived yet.",
        "problems": "Problems", "file": "File", "token": "API token expires",
        "token_never": "never", "token_unknown": "not checked yet", "in_days": "in {} days",
        "export_day": "Download the day as ZIP", "export_month": "Download the month as ZIP",
        "only_alarms": "Only days with an alarm", "no_alarm_days": "No day with an alarm.",
        "quarantine": "Quarantine",
        "quarantine_info": "Queue entries the app could not read. They lie in {} (reachable e.g. with Samba or "
                           "an editor app); the problem goes once they are deleted.",
        "moved": "Set aside", "missing_files": "Missing files", "reason": "Reason",
    },
    "de": {
        "overview": "Übersicht", "details": "Detailansicht", "queue": "Warteschlange",
        "settings": "Einstellungen", "back": "Zurück", "refresh": "Aktualisieren", "as_of": "Stand {}",
        "overview_info": "Status und Free-Tier-Verbrauch",
        "details_info": "Protokoll, Snapshots und Zusammenfassungen nach Monaten",
        "queue_info": "Objekte, die auf ihren Upload warten", "settings_info": "Optionen der App in Home Assistant",
        "status": "Status", "working": "Archivierung läuft", "broken": "Archivierung gestört",
        "r2": "R2-Verbindung", "reachable": "erreichbar",
        "unreachable": "nicht erreichbar", "unknown": "seit dem Start noch nicht benutzt", "waiting": "Wartende Objekte",
        "last_upload": "Letzter Upload", "last_summary": "Letzte Zusammenfassung",
        "free_tier": "Free-Tier (ganzes Konto)", "account": "Konto", "bucket": "Dieser Bucket",
        "free": "Free-Tier", "guard_limit": "Wächter stoppt bei", "month_end": "Monatsende (hochgerechnet)",
        "storage": "Speicher", "class_a": "Schreiben (Class A)", "class_b": "Lesen (Class B)",
        "growth": "Speicherzuwachs", "per_day": "pro Tag", "days_left": "Wächter-Grenze erreicht in",
        "days_unit": "Tagen",
        "never": "nicht absehbar", "no_figure": "Noch keine Verbrauchszahl von Cloudflare.",
        "guard_off": "Der Free-Tier-Wächter ist aus.", "day": "Tag", "days": "Tage", "entries": "Einträge",
        "log": "Protokoll", "snapshots": "Snapshots", "alarms": "Alarme", "summary": "Zusammenfassung",
        "summaries": "Zusammenfassungen", "none": "keine", "time": "Zeit", "message": "Meldung",
        "pictures": "Bilder", "states": "Zustände", "entity": "Entität",
        "state": "Zustand", "changed": "Geändert", "from_to": "Wechsel",
        "reconstructed": "Aus dem Recorder nachgebaut", "missing": "Fehlende Bilder",
        "download": "Herunterladen", "files": "Dateien", "empty_queue": "Nichts wartet.", "object": "Objekt",
        "kind": "Art", "size": "Größe", "age": "Wartet seit",
        "cached_only": "Die Free-Tier-Grenze ist erreicht: Es wird nur angezeigt, was schon gelesen wurde.",
        "not_cached": "(nicht im Zwischenspeicher)", "user": "Benutzer",
        "no_days": "Noch nichts archiviert.", "problems": "Probleme", "file": "Datei",
        "token": "API-Token läuft ab", "token_never": "nie", "token_unknown": "noch nicht geprüft",
        "in_days": "in {} Tagen", "export_day": "Tag als ZIP herunterladen",
        "export_month": "Monat als ZIP herunterladen", "only_alarms": "Nur Tage mit Alarm",
        "no_alarm_days": "Kein Tag mit Alarm.", "quarantine": "Quarantäne",
        "quarantine_info": "Einträge der Warteschlange, die die App nicht lesen konnte. Sie liegen in {} "
                           "(erreichbar z. B. über Samba oder eine Editor-App); die Meldung verschwindet, "
                           "sobald sie gelöscht sind.",
        "moved": "Beiseitegelegt", "missing_files": "Fehlende Dateien", "reason": "Grund",
    },
}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"],
}
WEEKDAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "de": ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."]}

PANEL_STATES = {
    "en": {"disarmed": "disarmed", "armed_home": "armed home", "armed_away": "armed away",
           "armed_night": "armed night", "armed_vacation": "armed vacation",
           "armed_custom_bypass": "armed custom bypass", "arming": "arming", "pending": "pending",
           "triggered": "ALARM", a.REQUEST: "on request"},
    "de": {"disarmed": "Unscharf", "armed_home": "Zuhause scharf", "armed_away": "Abwesend scharf",
           "armed_night": "Nacht scharf", "armed_vacation": "Urlaub scharf",
           "armed_custom_bypass": "Benutzerdefiniert scharf", "arming": "Wird scharf",
           "pending": "Eingangsverzögerung", "triggered": "ALARM", a.REQUEST: "Auf Anfrage"},
}

# The app's problems (see Archive.health) in German, by their code
PROBLEMS_DE = {
    "ha": "App ohne Verbindung zu Home Assistant",
    "r2": "Cloudflare R2 nicht erreichbar",
    "r2_check": "Prüfung von R2 fehlgeschlagen: {error}",
    "upload": "Letzter Upload fehlgeschlagen: {error}",
    "quarantine": "{count} Objekte in Quarantäne",
    "waiting": "{count} Objekte warten, das älteste seit {minutes} min",
    "limit": "Free-Tier-Grenze erreicht",
    "no_figure": "Keine Verbrauchszahl von Cloudflare, Uploads angehalten",
    "summary": "Tageszusammenfassung vom {day} fehlt",
}

# Material Design Icons (Apache 2.0), as Home Assistant draws them
ICONS = {
    "overview": "M19,5V7H15V5H19M9,5V11H5V5H9M19,13V19H15V13H19M9,17V19H5V17H9M21,3H13V9H21V3M11,3H3V13H11V3M21,11H13V21H21V11M11,15H3V21H11V15Z",
    "details": "M7 11H9V13H7V11M21 5V19C21 20.11 20.11 21 19 21H5C3.89 21 3 20.1 3 19V5C3 3.9 3.9 3 5 3H6V1H8V3H16V1H18V3H19C20.11 3 21 3.9 21 5M5 7H19V5H5V7M19 19V9H5V19H19M15 13V11H17V13H15M11 13V11H13V13H11M7 15H9V17H7V15M15 17V15H17V17H15M11 17V15H13V17H11Z",
    "queue": "M2 12H4V17H20V12H22V17C22 18.11 21.11 19 20 19H4C2.9 19 2 18.11 2 17V12M12 2L6.46 7.46L7.88 8.88L11 5.75V15H13V5.75L16.13 8.88L17.55 7.45L12 2Z",
    "settings": "M12,8A4,4 0 0,1 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12A4,4 0 0,1 12,8M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10M10,22C9.75,22 9.54,21.82 9.5,21.58L9.13,18.93C8.5,18.68 7.96,18.34 7.44,17.94L4.95,18.95C4.73,19.03 4.46,18.95 4.34,18.73L2.34,15.27C2.21,15.05 2.27,14.78 2.46,14.63L4.57,12.97L4.5,12L4.57,11L2.46,9.37C2.27,9.22 2.21,8.95 2.34,8.73L4.34,5.27C4.46,5.05 4.73,4.96 4.95,5.05L7.44,6.05C7.96,5.66 8.5,5.32 9.13,5.07L9.5,2.42C9.54,2.18 9.75,2 10,2H14C14.25,2 14.46,2.18 14.5,2.42L14.87,5.07C15.5,5.32 16.04,5.66 16.56,6.05L19.05,5.05C19.27,4.96 19.54,5.05 19.66,5.27L21.66,8.73C21.79,8.95 21.73,9.22 21.54,9.37L19.43,11L19.5,12L19.43,13L21.54,14.63C21.73,14.78 21.79,15.05 21.66,15.27L19.66,18.73C19.54,18.95 19.27,19.04 19.05,18.95L16.56,17.95C16.04,18.34 15.5,18.68 14.87,18.93L14.5,21.58C14.46,21.82 14.25,22 14,22H10M11.25,4L10.88,6.61C9.68,6.86 8.62,7.5 7.85,8.39L5.44,7.35L4.69,8.65L6.8,10.2C6.4,11.37 6.4,12.64 6.8,13.8L4.68,15.36L5.43,16.66L7.86,15.62C8.63,16.5 9.68,17.14 10.87,17.38L11.24,20H12.76L13.13,17.39C14.32,17.14 15.37,16.5 16.14,15.62L18.57,16.66L19.32,15.36L17.2,13.81C17.6,12.64 17.6,11.37 17.2,10.2L19.31,8.65L18.56,7.35L16.15,8.39C15.38,7.5 14.32,6.86 13.12,6.62L12.75,4H11.25Z",
    "refresh": "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
    "next": "M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z",
    "back": "M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z",
    "open": "M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z",
    "download": "M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z",
    "zip": "M14,17H12V15H10V13H12V15H14M14,9H12V11H14V13H12V11H10V9H12V7H10V5H12V7H14M19,3H5C3.89,3 3,3.89 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V5C21,3.89 20.1,3 19,3Z",
    "check": "M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z",
    "ok": "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z",
    "alert": "M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
}
# Icon backgrounds in the menu, in the manner of Home Assistant's settings
MENU_COLORS = {"overview": "#5A87FA", "details": "#E48629", "queue": "#518C43", "settings": "#4A5963"}


def icon(name: str, cls: str = "") -> str:
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="{ICONS[name]}"/></svg>'


# The look of Home Assistant, the same file in every app of this kind (see its
# header); the script below replaces its colors with the theme in use
HA_CSS = (Path(__file__).resolve().parents[2] / "local/share/ha-app/ha.css").read_text()
# The language of Home Assistant's interface for the page (cookie ha_lang),
# shared like ha.css; it runs first, in the page's one script
HA_LANG_JS = (Path(__file__).resolve().parents[2] / "local/share/ha-app/ha-lang.js").read_text()
# What only this page needs, after ha.css
STYLE = """
header > .back { margin: 0 8px 0 -12px; }
main.narrow { max-width: 632px; }
main.wide { max-width: none; padding: 0; }
.item { display: flex; align-items: center; gap: 16px; min-height: 66px; padding: 12px 16px;
  color: var(--primary-text-color); }
.item:hover, a.row:hover { background: var(--ha-hover); }
.icon-bg { display: flex; padding: 8px; border-radius: 50%; color: #fff; }
.text { display: flex; flex: 1; flex-direction: column; min-width: 0; }
.headline { line-height: 20px; }
.supporting { font-size: var(--ha-font-size-s); line-height: 16px; color: var(--secondary-text-color); margin-top: 2px; }
.next { color: var(--secondary-text-color); }
.badge { min-width: 20px; height: 20px; padding: 0 6px; border-radius: 10px; font-size: var(--ha-font-size-s);
  font-weight: var(--ha-font-weight-medium); line-height: 20px; text-align: center; color: var(--text-primary-color);
  background: var(--primary-color); }
.badge.warn { background: var(--warning-color); }
ul.problems { margin: 0 0 12px; padding-left: 32px; }
pre { margin: 8px 0 0; padding: 12px; overflow-x: auto; border-radius: 8px; font-size: var(--ha-font-size-s);
  background: var(--secondary-background-color); }
.table { font-size: 13px; }
.row { display: grid; grid-template-columns: var(--cols); column-gap: 16px; align-items: center;
  min-height: 52px; padding: 0 16px; color: var(--primary-text-color); border-top: 1px solid var(--divider-color); }
.row > * { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.thead { min-height: 56px; font-size: var(--ha-font-size-s); font-weight: var(--ha-font-weight-medium);
  border-top: 0; border-bottom: 1px solid var(--divider-color); }
.thead > * { white-space: nowrap; }
.thead + .row { border-top: 0; }
.row.empty { color: var(--secondary-text-color); }
summary.row { list-style: none; cursor: pointer; padding-left: 12px; font-weight: var(--ha-font-weight-medium);
  background: var(--secondary-background-color); }
details > summary::-webkit-details-marker { display: none; }
summary .group { display: flex; align-items: center; gap: 12px; white-space: nowrap; }
details:not([open]) .open { transform: rotate(180deg); }
summary a.next:hover { color: var(--primary-color); }
.cols-details { --cols: minmax(0, 2fr) repeat(4, minmax(0, 1fr)) 24px; }
.cols-snapshots { --cols: minmax(0, 2fr) minmax(0, 2fr) minmax(0, 1fr) 24px; }
.cols-log { --cols: 140px minmax(0, 1fr); }
.cols-files { --cols: minmax(0, 1fr) 24px; }
.cols-queue { --cols: minmax(0, 3fr) minmax(0, 1fr) minmax(0, 1fr) minmax(0, 2fr); }
.alarm { color: var(--error-color); font-weight: var(--ha-font-weight-medium); }
.button svg { width: 18px; height: 18px; }
.pics { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); gap: 16px; }
.pics figure { margin: 0; } .pics img { display: block; width: 100%; border-radius: 12px; }
.pics figcaption { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
main.wide .toolbar { min-height: 56px; padding: 8px 16px; border-bottom: 1px solid var(--divider-color); }
main.wide .alert { margin: 16px; }
.chip svg { width: 18px; height: 18px; }
@media (max-width: 600px) {
  .cols-details { --cols: minmax(0, 5fr) repeat(3, minmax(0, 3fr)) 24px; }
  .cols-log { --cols: 64px minmax(0, 1fr); }
  .cols-queue { --cols: minmax(0, 3fr) minmax(0, 1fr) minmax(0, 2fr); }
}
"""

# Takes Home Assistant's theme from the page around the ingress frame (same
# origin), and follows it when the theme or dark mode changes; the refresh
# button in the header reloads the page
THEME_VARS = ("primary-background-color", "secondary-background-color", "card-background-color",
              "primary-text-color", "secondary-text-color", "disabled-text-color", "divider-color",
              "primary-color", "text-primary-color", "success-color", "warning-color", "error-color",
              "info-color", "sidebar-background-color", "sidebar-text-color",
              "sidebar-icon-color", "app-header-border-bottom", "ha-card-border-radius", "ha-card-border-width",
              "ha-card-border-color", "ha-card-box-shadow", "ha-font-family-body", "header-height")
SCRIPT = """(() => {
  try {
    const outer = window.parent.document.documentElement;
    if (outer !== document.documentElement) {
      const names = NAMES;
      const take = () => {
        const theme = getComputedStyle(outer), own = document.documentElement.style;
        for (const n of names) { const v = theme.getPropertyValue("--" + n).trim(); if (v) own.setProperty("--" + n, v); }
        const ha = window.parent.document.querySelector("home-assistant");
        if (ha && ha.hass) own.colorScheme = ha.hass.themes.darkMode ? "dark" : "light";
      };
      take();
      new MutationObserver(take).observe(outer, { attributes: true, attributeFilter: ["style", "class"] });
    }
  } catch (e) {}
  document.addEventListener("click", (e) => {
    if (e.target.closest("#refresh")) { e.preventDefault(); location.reload(); }
  });
})();""".replace("NAMES", json.dumps(THEME_VARS))
SCRIPT = HA_LANG_JS + SCRIPT
# Only the page's own inline script runs, pictures come from the page itself;
# the inline styles are the page's own (style-src without a hash: the menu's
# icon colors are style attributes)
CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
       f"script-src 'sha256-{base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()}'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


def lang_of(cookie: str | None, configured: str | None = None, accept: str | None = None) -> str:
    """German or English, as Home Assistant's interface shows it to the viewer.

    The page script keeps the viewer's language in the cookie ha_lang
    (share/ha-app/ha-lang.js): German for "de", English for everything else.
    Without that cookie, Home Assistant's configured language; only while that
    is not known yet, German if the browser puts German before English.
    """
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "ha_lang":
            return "de" if value.strip().lower() == "de" else "en"
    if configured:
        return "de" if configured.lower().startswith("de") else "en"
    for part in (accept or "").split(","):
        code = part.split(";")[0].strip().lower()
        if code.startswith("de"):
            return "de"
        if code.startswith("en"):
            return "en"
    return "en"


# The language of the request being answered (set by Web._language)
REQUEST_LANG: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_lang", default=None)


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
    """A problem of Archive.health in the page's language."""
    code = getattr(p, "code", None)
    if lang != "de" or code not in PROBLEMS_DE:
        return str(p)
    params = dict(p.params)
    if code == "summary":
        params["day"] = date.fromisoformat(str(params["day"])).strftime("%d.%m.%Y")
    return PROBLEMS_DE[code].format(**params)


class Cache:
    """Objects read from R2, as files named after their key; the oldest go
    when the cache grows beyond its size."""

    def __init__(self, folder: Path, max_bytes: int = CACHE_BYTES) -> None:
        self.folder = folder
        self._max = max_bytes
        folder.mkdir(parents=True, exist_ok=True)
        # Its total size, counted once here and then kept up to date
        self._total = sum(p.stat().st_size for p in self._files())

    def _files(self) -> list[Path]:
        return [p for p in self.folder.iterdir() if p.is_file() and not p.name.startswith(".")]

    def _path(self, key: str) -> Path:
        return self.folder / quote(key, safe="")

    def has(self, key: str) -> bool:
        return self._path(key).is_file()

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
            os.utime(path)
        except OSError:
            return None
        return data

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        with contextlib.suppress(OSError):
            self._total -= path.stat().st_size
        write_atomic(path, data)
        self._total += len(data)
        if self._total <= self._max:
            return
        # Over the size: the least recently read go first
        for old in sorted(self._files(), key=lambda p: p.stat().st_mtime):
            if self._total <= self._max:
                break
            with contextlib.suppress(OSError):
                size = old.stat().st_size
                old.unlink()
                self._total -= size


def host_slug() -> str | None:
    """The app's slug, e.g. 4a183770_r2_alarm_archive: the Supervisor names
    the container's host after it; none outside the Supervisor's containers."""
    return os.environ.get("HOSTNAME", "").replace("-", "_") or None


def valid_day(day: str) -> bool:
    """A day as YYYY-MM-DD, and a real one."""
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return False
    try:
        date.fromisoformat(day)
    except ValueError:
        return False
    return True


class Blocked(Exception):
    """Reading from R2 would pass the free tier limit."""


class Web:
    def __init__(self, archive, cache: Cache, allowed_peers: set[str] | None = None,
                 slug: str | None = None, version: str = "") -> None:
        self.archive = archive
        self.cache = cache
        self.allowed = {INGRESS_PEER} if allowed_peers is None else allowed_peers
        # The app's slug for the link to its options; none outside the Supervisor
        self.slug = slug
        # The app's version (config.yaml, given to the image when it is built), shown on every page
        self.version = version

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._only_ingress, self._security_headers, self._language])
        app.router.add_get("/", self.menu)
        app.router.add_get("/overview", self.overview)
        app.router.add_get("/details", self.details)
        app.router.add_get("/day/{day}", self.day)
        app.router.add_get("/snapshot/{day}/{name}", self.snapshot)
        app.router.add_get("/queue", self.queue)
        app.router.add_get("/file/{key:.+}", self.file)
        app.router.add_get("/export/{period}.zip", self.export)
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

    @web.middleware
    async def _security_headers(self, request: web.Request, handler):
        response = await handler(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @web.middleware
    async def _language(self, request: web.Request, handler):
        REQUEST_LANG.set(lang_of(request.headers.get("Cookie"), self.archive.language,
                                 request.headers.get("Accept-Language")))
        return await handler(request)

    # --- Helpers -------------------------------------------------------------------
    def t(self, key: str) -> str:
        return TEXT[self.lang][key]

    @property
    def lang(self) -> str:
        return REQUEST_LANG.get() or lang_of(None, self.archive.language)

    @property
    def tz(self):
        return self.archive.tz

    def local(self, value) -> datetime | None:
        if not value:
            return None
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return moment.astimezone(self.tz) if self.tz else moment

    def when(self, value) -> str:
        local = self.local(value)
        if local is None:
            return "–"
        return local.strftime("%d.%m.%Y %H:%M:%S" if self.lang == "de" else "%Y-%m-%d %H:%M:%S")

    def clock(self, value) -> str:
        local = self.local(value)
        return local.strftime("%H:%M:%S") if local else "–"

    def panel_state(self, state) -> str:
        if state is None:
            return "–"
        return PANEL_STATES[self.lang].get(str(state), str(state))

    async def reason(self, folder: str) -> str | None:
        """The reason of a snapshot an automation asked for, from its meta file."""
        try:
            return json.loads(await self.read(folder + "/meta.json")).get("reason") or None
        except (Blocked, R2Error, ValueError, AttributeError):
            return None

    async def user_name(self, user_id: str | None) -> str:
        """The person of a Home Assistant user, else the ID."""
        if not user_id:
            return "–"
        try:
            for state in await self.archive.ha.states():
                if state["entity_id"].startswith("person.") and \
                        (state.get("attributes") or {}).get("user_id") == user_id:
                    return str(state["attributes"].get("friendly_name") or state["entity_id"])
            # Users without a person, e.g. a wall display's login
            for user in await self.archive.ha.call({"type": "config/auth/list"}):
                if user.get("id") == user_id and user.get("name"):
                    return str(user["name"])
        except Exception as err:  # the page still shows the ID
            LOGGER.debug("No name for %s: %s", user_id, err)
        return user_id

    def day_text(self, day: str, weekday: bool = False) -> str:
        d = date.fromisoformat(day)
        text = d.strftime("%d.%m.%Y") if self.lang == "de" else day
        return f"{WEEKDAYS[self.lang][d.weekday()]} {text}" if weekday else text

    def month_text(self, month: str) -> str:
        year, number_ = month.split("-")
        return f"{MONTHS[self.lang][int(number_) - 1]} {year}"

    def page(self, request: web.Request, title: str, body: str, back: str | None = None,
             width: str = "") -> web.Response:
        base = html.escape(request.headers.get("X-Ingress-Path", ""), quote=True)
        body = body.replace("{base}", base)
        back_link = (f'<a class="icon-button back" href="{base}{back}" title="{self.t("back")}">{icon("back")}</a>'
                     if back is not None else "")
        return web.Response(content_type="text/html", charset="utf-8", text=(
            f'<!doctype html><html lang="{self.lang}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{HA_CSS}</style><style>{STYLE}</style>"
            f"<script>{SCRIPT}</script></head><body>"
            f"<header>{back_link}<h1>{html.escape(title)}</h1>"
            f'<div class="actions">{self.header_actions()}</div></header>'
            f'<main class="{width}">{body}</main>{self.about()}</body></html>'))

    def header_actions(self) -> str:
        """The same three on every page: when the data were read, refresh,
        and the app's options in Home Assistant."""
        stamp = html.escape(self.t("as_of").format(self.clock(self.archive.now())))
        actions = (f'<span class="stamp opt">{stamp}</span>'
                   f'<a class="icon-button" id="refresh" href="" role="button" title="{self.t("refresh")}">'
                   f'{icon("refresh")}</a>')
        # The options are Home Assistant's own page, outside the ingress frame
        if self.slug:
            actions += (f'<a class="icon-button" href="/config/app/{html.escape(self.slug, quote=True)}/config" '
                        f'target="_top" title="{self.t("settings")}">{icon("settings")}</a>')
        return actions

    def about(self) -> str:
        """The app and its version, at the bottom of every page."""
        return f'<footer class="version">{html.escape(f"{APP_NAME} {self.version}".strip())}</footer>'

    @staticmethod
    def alert(text: str) -> str:
        """A warning like ha-alert."""
        return f'<div class="alert warning">{icon("alert")}<div>{html.escape(text)}</div></div>'

    def card(self, title: str | None, content: str, padded: bool = True, actions: str = "") -> str:
        head = f"<h2>{html.escape(title)}</h2>" if title else ""
        inner = f'<div class="content">{content}</div>' if padded else content
        foot = f'<div class="card-actions">{actions}</div>' if actions else ""
        return f'<div class="card">{head}{inner}{foot}</div>'

    def table(self, cols: str, head: list[tuple[str, str]], rows: list[str], empty: str) -> str:
        """A table like Home Assistant's data table; head cells are (text, class)."""
        header = "".join(f'<div class="{cls}">{html.escape(text)}</div>' for text, cls in head)
        body = "".join(rows) or f'<div class="row empty"><div>{html.escape(empty)}</div></div>'
        return f'<div class="table cols-{cols}"><div class="row thead">{header}</div>{body}</div>'

    async def read(self, key: str, keep: bool = True) -> bytes:
        """An object from the cache, or from R2 past the guard; `keep`: put
        what R2 gave into the cache (not for an export, it would push out
        everything else)."""
        if (data := self.cache.get(key)) is not None:
            return data
        if not self.allows_reads(1):
            raise Blocked(key)
        data = await self.archive.r2.get(key)
        self.archive.guard.count(class_b=1)
        if keep:
            self.cache.put(key, data)
        return data

    def allows_reads(self, count: int) -> bool:
        archive = self.archive
        return archive.guard.allows(asyncio.get_running_loop().time(), utc_month(archive.now()),
                                    class_b=count)

    def day_keys(self, day: str) -> tuple[list[str], list[str]]:
        """(log keys, snapshot folders) of a day from the local index."""
        keys = [k for k in self.archive.state.data["uploaded"].get(day, []) if a.valid_key(k)]
        logs = sorted(k for k in keys if k.startswith(a.PREFIX["log"]))
        folders = sorted({k.rsplit("/", 1)[0] for k in keys if k.startswith(a.PREFIX["snapshots"])})
        return logs, folders

    @staticmethod
    def folder_state(folder: str) -> str:
        return folder.rsplit("/", 1)[1].split("_", 1)[1]

    # --- Pages -----------------------------------------------------------------------
    async def menu(self, request: web.Request) -> web.Response:
        problems = self.archive.health()
        waiting = len(self.archive.queue)
        items = [("overview", "/overview", len(problems), True), ("details", "/details", 0, False),
                 ("queue", "/queue", waiting, False)]
        if self.slug:
            items.append(("settings", f"/config/app/{self.slug}/config", 0, False))
        rows = []
        for name, href, count, warn in items:
            # The settings are Home Assistant's own page, outside the ingress frame
            link = f'href="{href}" target="_top"' if name == "settings" else f'href="{{base}}{href}"'
            badge = f'<span class="badge{" warn" if warn else ""}">{count}</span>' if count else ""
            rows.append(f'<a class="item" {link}><span class="icon-bg" style="background:{MENU_COLORS[name]}">'
                        f'{icon(name)}</span><span class="text"><span class="headline">{self.t(name)}</span>'
                        f'<span class="supporting">{self.t(name + "_info")}</span></span>{badge}'
                        f'{icon("next", "next")}</a>')
        return self.page(request, APP_NAME, self.card(None, "".join(rows), padded=False), width="narrow")

    async def overview(self, request: web.Request) -> web.Response:
        archive = self.archive
        problems = archive.health()
        status = archive.status()
        state = (f'<div class="state ok">{icon("ok")}{self.t("working")}</div>' if not problems else
                 f'<div class="state bad">{icon("alert")}{self.t("broken")}</div><ul class="problems">'
                 + "".join(f"<li>{html.escape(problem_text(p, self.lang))}</li>" for p in problems) + "</ul>")
        r2 = {True: self.t("reachable"), False: self.t("unreachable"), None: self.t("unknown")}
        body = self.card(self.t("status"), (
            f'{state}<table class="kv">'
            f"<tr><th>{self.t('r2')}</th><td>{r2[archive.r2_connected]}</td></tr>"
            f"<tr><th>{self.t('waiting')}</th><td>{len(archive.queue)}</td></tr>"
            f"<tr><th>{self.t('last_upload')}</th><td>{self.when(status['last_upload'])}</td></tr>"
            f"<tr><th>{self.t('last_summary')}</th><td>{self.when(status['last_summary'])}</td></tr>"
            + (f"<tr><th>{self.t('token')}</th><td>{self.token_text()}</td></tr>" if archive.cloudflare else "")
            + "</table>"))
        body += self.card(self.t("free_tier"), self.free_tier())
        return self.page(request, self.t("overview"), body, back="/")

    def token_text(self) -> str:
        archive = self.archive
        if not archive.token_checked:
            return f'<span class="muted">{self.t("token_unknown")}</span>'
        if archive.token_expiry is None:
            return self.t("token_never")
        days = (archive.token_expiry - archive.now()).days
        text = f"{self.when(archive.token_expiry)} ({self.t('in_days').format(max(days, 0))})"
        return f'<span class="bad">{text}</span>' if archive.token_expiring() else text

    def free_tier(self) -> str:
        archive = self.archive
        guard = archive.guard
        notes = "" if guard.enabled else f'<p class="muted">{self.t("guard_off")}</p>'
        totals = guard.totals()
        if totals is None:
            return f"{notes}<p class=\"muted\">{self.t('no_figure')}</p>"
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
            rows.append(f"<tr><td>{self.t(name)}</td><td class='n'>{number(value, lang)}</td>"
                        f"<td class='n'>{number(own, lang)}</td><td class='n'>{number(free, lang)}</td>"
                        f"<td class='n'>{percent(100 * value / free, lang)}</td><td class='n'>{number(LIMITS[key], lang)}</td>"
                        f"<td class='n'>{number(value / elapsed, lang)}</td></tr>")
        storage = totals["storage"]
        rows.append(f"<tr><td>{self.t('storage')}</td><td class='n'>{size_text(storage, lang)}</td>"
                    f"<td class='n'>{size_text(usage.bucket_storage_bytes, lang)}</td>"
                    f"<td class='n'>{size_text(FREE_STORAGE, lang)}</td><td class='n'>{percent(100 * storage / FREE_STORAGE, lang)}</td>"
                    f"<td class='n'>{size_text(LIMITS['storage'], lang)}</td><td class='n'>–</td></tr>")
        growth = self.storage_growth()
        if growth is None:
            outlook = ""
        else:
            left = (LIMITS["storage"] - storage) / growth if growth > 0 else None
            outlook = ('<table class="kv">'
                       f"<tr><th>{self.t('growth')}</th><td>{size_text(max(growth, 0), lang)} {self.t('per_day')}</td></tr>"
                       f"<tr><th>{self.t('days_left')}</th><td>"
                       + (f"{left:.0f} {self.t('days_unit')}" if left is not None and left < 3650 else self.t("never"))
                       + "</td></tr></table>")
        return (f"{notes}<div class='scroll'><table><tr><th></th>"
                f"<th class='n'>{self.t('account')}</th><th class='n'>{self.t('bucket')}</th>"
                f"<th class='n'>{self.t('free')}</th><th class='n'>%</th><th class='n'>{self.t('guard_limit')}</th>"
                f"<th class='n'>{self.t('month_end')}</th></tr>{''.join(rows)}</table></div>{outlook}")

    def storage_growth(self) -> float | None:
        """Bytes per day of the whole account over the last week of samples."""
        days = sorted(self.archive.state.data["storage_days"].items())[-8:]
        if len(days) < 2:
            return None
        (first, (start, _)), (last, (end, _)) = days[0], days[-1]
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days
        return (end - start) / span if span else None

    def day_counts(self, day: str) -> tuple[int, int, int, int]:
        """(entries, snapshots, alarms, summary files) of a day from the index."""
        logs, folders = self.day_keys(day)
        alarms = sum(1 for f in folders if self.folder_state(f) == "triggered")
        files = (self.archive.state.data["summaries"].get(day) or {}).get("files", 0)
        return len(logs), len(folders), alarms, files

    async def details(self, request: web.Request) -> web.Response:
        data = self.archive.state.data
        only_alarms = request.query.get("alarms") == "1"
        all_days = sorted(set(data["uploaded"]) | set(data["summaries"]), reverse=True)
        if only_alarms:
            all_days = [day for day in all_days if self.day_counts(day)[2]]
        months: dict[str, list[str]] = {}
        for day in all_days:
            months.setdefault(day[:7], []).append(day)
        # The newest month opens, or the one a day page came back from; filtered, all
        wanted = request.query.get("month")
        open_month = wanted if wanted in months else next(iter(months), None)
        groups = []
        for month, days in months.items():
            counts = [self.day_counts(day) for day in days]
            rows = "".join(
                f'<a class="row" href="{{base}}/day/{quote(day)}"><div>{self.day_text(day, weekday=True)}</div>'
                f"<div class='n'>{entries}</div><div class='n'>{snaps}</div>"
                f"<div class='n{' alarm' if alarms else ''}'>{alarms or '–'}</div>"
                f"<div class='n opt'>{files or '–'}</div>{icon('next', 'next')}</a>"
                for day, (entries, snaps, alarms, files) in zip(days, counts, strict=True))
            alarms = sum(c[2] for c in counts)
            groups.append(
                f'<details id="m-{month}"{" open" if only_alarms or month == open_month else ""}><summary class="row">'
                f'<div class="group">{icon("open", "open")}{self.month_text(month)}'
                f'<span class="muted opt">· {len(days)} {self.t("days")}</span></div>'
                f"<div class='n'>{sum(c[0] for c in counts)}</div><div class='n'>{sum(c[1] for c in counts)}</div>"
                f"<div class='n{' alarm' if alarms else ''}'>{alarms or '–'}</div>"
                f"<div class='n opt'>{sum(c[3] for c in counts) or '–'}</div>"
                f'<a class="next" href="{{base}}/export/{month}.zip" title="{self.t("export_month")}">'
                f'{icon("download")}</a></summary>{rows}</details>')
        head = (f'<div class="row thead"><div>{self.t("day")}</div><div class="n">{self.t("entries")}</div>'
                f'<div class="n">{self.t("snapshots")}</div><div class="n">{self.t("alarms")}</div>'
                f'<div class="n opt">{self.t("summaries")}</div><div></div></div>')
        empty = self.t("no_alarm_days" if only_alarms else "no_days")
        body = "".join(groups) or f'<div class="row empty"><div>{empty}</div></div>'
        chip = (f'<a class="chip{" on" if only_alarms else ""}" href="{{base}}/details{"" if only_alarms else "?alarms=1"}">'
                + (icon("check") if only_alarms else "") + f'{self.t("only_alarms")}</a>')
        return self.page(request, self.t("details"), f'<div class="toolbar">{chip}</div>'
                         f'<div class="table cols-details">{head}{body}</div>', back="/", width="wide")

    async def day(self, request: web.Request) -> web.Response:
        day = request.match_info["day"]
        if not valid_day(day):
            raise web.HTTPNotFound()
        logs, folders = self.day_keys(day)
        blocked = False
        lines = []
        for key in logs:
            try:
                entry = json.loads(await self.read(key))
                lines.append((entry.get("time", ""), f'<div class="row"><div class="nowrap">{self.clock(entry.get("time"))}</div>'
                              f"<div>{html.escape(str(entry.get('message', '')))}</div></div>"))
            except Blocked:
                blocked = True
                lines.append(("", f'<div class="row"><div></div><div class="muted">{html.escape(key)} '
                                  f'{self.t("not_cached")}</div></div>'))
            except (R2Error, ValueError) as err:
                lines.append(("", f'<div class="row"><div></div><div class="bad">{html.escape(str(err))}</div></div>'))
        log = self.table("log", [(self.t("time"), ""), (self.t("message"), "")],
                         [row for _, row in sorted(lines)], self.t("none"))
        keys = self.archive.state.data["uploaded"].get(day, [])
        snaps = []
        for folder in folders:
            name = folder.rsplit("/", 1)[1]
            state = self.folder_state(folder)
            pictures = sum(1 for k in keys if k.startswith(folder + "/") and k.endswith(".jpg"))
            label = html.escape(self.panel_state(state))
            if state == a.REQUEST and (reason := await self.reason(folder)):
                label += f' <span class="muted">· {html.escape(reason)}</span>'
            snaps.append(f'<a class="row" href="{{base}}/snapshot/{day}/{quote(name, safe="")}">'
                         f'<div class="nowrap">{self.clock(self.folder_time(folder))}</div>'
                         f"<div class='{'alarm' if state == 'triggered' else ''}'>{label}</div>"
                         f"<div class='n'>{pictures or '–'}</div>{icon('next', 'next')}</a>")
        snapshots = self.table("snapshots", [(self.t("time"), ""), (self.t("state"), ""), (self.t("pictures"), "n"),
                                             ("", "")], snaps, self.t("none"))
        files = (self.archive.state.data["summaries"].get(day) or {}).get("files", 0)
        rows = []
        for part in range(files):
            key = a.summary_key(day, part)
            rows.append(f'<a class="row" href="{{base}}/file/{key}?download=1" title="{self.t("download")}">'
                        f'<div>{html.escape(key.rsplit("/", 1)[1])}</div>{icon("download", "next")}</a>')
        summaries = self.table("files", [(self.t("file"), ""), ("", "")], rows, self.t("none"))
        note = self.alert(self.t("cached_only")) if blocked else ""
        export = f'<a class="button" href="{{base}}/export/{day}.zip">{icon("zip")}{self.t("export_day")}</a>'
        body = (note + self.card(self.t("log"), log, padded=False) + self.card(self.t("snapshots"), snapshots, padded=False)
                + self.card(self.t("summary"), summaries, padded=False, actions=export))
        return self.page(request, self.day_text(day, weekday=True), body, back=f"/details?month={day[:7]}")

    @staticmethod
    def folder_time(folder: str) -> datetime | None:
        stamp = folder.rsplit("/", 1)[1].split("_", 1)[0]
        try:
            return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
        except ValueError:
            return None

    async def snapshot(self, request: web.Request) -> web.Response:
        day, name = request.match_info["day"], request.match_info["name"]
        if not valid_day(day):
            raise web.HTTPNotFound()
        folder = f"{a.PREFIX['snapshots']}{a.day_folder(day)}{name}"
        keys = [k for k in self.archive.state.data["uploaded"].get(day, [])
                if k.startswith(folder + "/") and a.valid_key(k)]
        if not keys or not a.valid_folder(folder):
            raise web.HTTPNotFound()
        # The folder has the app's form (no quotes or brackets); encoded all the same
        link = html.escape(quote(folder, safe="/"), quote=True)
        back = f"/day/{day}"
        try:
            meta = json.loads(await self.read(folder + "/meta.json"))
            states = json.loads(gzip.decompress(await self.read(folder + "/zustand.json.gz")))
        except Blocked:
            return self.page(request, self.when(self.folder_time(folder)),
                             self.alert(self.t("cached_only")), back=back)
        rows = "".join(
            f"<tr><td>{html.escape(str((s.get('attributes') or {}).get('friendly_name') or s['entity_id']))}"
            f"<br><span class='muted'>{html.escape(s['entity_id'])}</span></td>"
            f"<td>{html.escape(str(s.get('state')))}</td><td class='nowrap'>{self.when(s.get('last_changed'))}</td></tr>"
            for s in states.get("states", []))
        # A series shows its pictures that arrived; older snapshots have one per camera
        present = {k.rsplit("/", 1)[1] for k in keys}
        shots = [(cam, file, n) for cam, first in (meta.get("cameras") or {}).items()
                 for n, file in enumerate((meta.get("series") or {}).get(cam) or [first], 1) if file in present]
        pictures = "".join(
            f'<figure><img src="{{base}}/file/{link}/{quote(file)}" alt="{html.escape(cam)}">'
            f'<figcaption><span>{html.escape(cam)}{f" · {n}" if n > 1 or len((meta.get("series") or {}).get(cam) or []) > 1 else ""}</span>'
            f'<a class="button" href="{{base}}/file/{link}/{quote(file)}?download=1">'
            f"{icon('download')}{self.t('download')}</a></figcaption></figure>"
            for cam, file, n in shots)
        missing = meta.get("missing_cameras") or []
        downloads = "".join(f'<a class="button" href="{{base}}/file/{link}/{f}?download=1">{icon("download")}{f}</a>'
                            for f in ("meta.json", "zustand.json.gz"))
        info = ('<table class="kv">'
                f"<tr><th>{self.t('from_to')}</th><td>{html.escape(self.panel_state(meta.get('from')))} → "
                f"{html.escape(self.panel_state(meta.get('to')))}</td></tr>"
                + (f"<tr><th>{self.t('reason')}</th><td>{html.escape(str(meta['reason']))}</td></tr>"
                   if meta.get("reason") else "")
                + f"<tr><th>{self.t('user')}</th><td>{html.escape(await self.user_name(meta.get('user_id')))}</td></tr>"
                + (f"<tr><th>{self.t('missing')}</th><td>{html.escape(', '.join(missing))}</td></tr>" if missing else "")
                + (f"<tr><th>{self.t('reconstructed')}</th><td>✓</td></tr>" if meta.get("reconstructed") else "")
                + f"<tr><th>{self.t('files')}</th><td>{downloads}</td></tr></table>")
        title = f"{self.when(meta.get('time'))} · {self.panel_state(meta.get('to'))}"
        body = (self.card(None, info)
                + (self.card(self.t("pictures"), f"<div class='pics'>{pictures}</div>") if pictures else "")
                + self.card(self.t("states"), f"<div class='scroll'><table><tr><th>{self.t('entity')}</th>"
                                              f"<th>{self.t('state')}</th><th>{self.t('changed')}</th></tr>{rows}</table></div>"))
        return self.page(request, title, body, back=back)

    async def queue(self, request: web.Request) -> web.Response:
        now = time.time()
        rows = [f'<div class="row"><div title="{html.escape(i.key)}">{html.escape(i.key)}</div>'
                f'<div class="opt">{html.escape(i.kind)}</div><div class="n">{size_text(i.size, self.lang)}</div>'
                f"<div>{self.when(datetime.fromtimestamp(i.queued, UTC))} ({int((now - i.queued) // 60)} min)</div></div>"
                for i in self.archive.queue.items()]
        table = self.table("queue", [(self.t("object"), ""), (self.t("kind"), "opt"), (self.t("size"), "n"),
                                     (self.t("age"), "")], rows, self.t("empty_queue"))
        if kept := self.archive.queue.quarantined():
            where = f"/addon_configs/{self.slug}/quarantine" if self.slug else str(self.archive.queue.quarantine)
            rows = [f'<div class="row"><div>{html.escape(q.name)}</div><div class="opt"></div>'
                    f'<div class="n">{size_text(q.size, self.lang)}</div>'
                    f"<div>{self.when(datetime.fromtimestamp(q.moved, UTC))}</div></div>" for q in kept]
            table += (f'<div class="toolbar"><strong>{self.t("quarantine")}</strong></div>'
                      + self.alert(self.t("quarantine_info").format(where))
                      + self.table("queue", [(self.t("file"), ""), ("", "opt"), (self.t("size"), "n"),
                                             (self.t("moved"), "")], rows, ""))
        return self.page(request, self.t("queue"), table, back="/", width="wide")

    async def export(self, request: web.Request) -> web.StreamResponse:
        """A day (YYYY-MM-DD) or a month (YYYY-MM) as one ZIP: every archived
        object under its key, and per day the log as readable text. Objects
        R2 no longer has (e.g. after the retention) are listed instead.

        The ZIP is built in a temporary file in /data and then streamed, so a
        month of pictures never sits in memory; objects are read from R2 a few
        at a time and not put into the page's cache."""
        period = request.match_info["period"]
        data = self.archive.state.data
        if not (valid_day(period) or valid_day(period + "-01")) or len(period) not in (7, 10):
            raise web.HTTPNotFound()
        days = sorted(d for d in set(data["uploaded"]) | set(data["summaries"]) if d.startswith(period))
        if not days:
            raise web.HTTPNotFound()
        plan = []
        for day in days:
            logs, _ = self.day_keys(day)
            keys = sorted(k for k in data["uploaded"].get(day, []) if a.valid_key(k))
            keys += [a.summary_key(day, part) for part in range((data["summaries"].get(day) or {}).get("files", 0))]
            plan.append((day, set(logs), keys))
        # Better no ZIP than half of one: the guard must allow every read up front
        if not self.allows_reads(sum(1 for _, _, keys in plan for k in keys if not self.cache.has(k))):
            raise web.HTTPServiceUnavailable(text=self.t("cached_only"))
        root = f"alarm-archiv-{period}"
        folder = self.cache.folder.parent
        with tempfile.TemporaryFile(dir=folder, prefix=".export-") as tmp:
            try:
                await self._write_zip(tmp, root, plan)
            except Blocked as err:
                raise web.HTTPServiceUnavailable(text=self.t("cached_only")) from err
            size = tmp.tell()
            tmp.seek(0)
            response = web.StreamResponse(headers={
                "Content-Type": "application/zip", "Content-Length": str(size),
                "Content-Disposition": f'attachment; filename="{root}.zip"'})
            await response.prepare(request)
            while chunk := tmp.read(1 << 20):
                await response.write(chunk)
            await response.write_eof()
        return response

    async def _write_zip(self, tmp, root: str, plan: list[tuple[str, set[str], list[str]]]) -> None:
        missing: list[str] = []
        limit = asyncio.Semaphore(EXPORT_READS)

        async def fetch(key: str) -> bytes | None:
            async with limit:
                try:
                    return await self.read(key, keep=False)
                except R2Error:
                    return None

        with zipfile.ZipFile(tmp, "w") as out:
            for day, logs, keys in plan:
                entries = []
                for start in range(0, len(keys), EXPORT_READS):
                    chunk = keys[start:start + EXPORT_READS]
                    for key, body in zip(chunk, await asyncio.gather(*map(fetch, chunk)), strict=True):
                        if body is None:
                            missing.append(key)
                            continue
                        packed = zipfile.ZIP_STORED if key.endswith((".jpg", ".gz")) else zipfile.ZIP_DEFLATED
                        out.writestr(f"{root}/{key}", body, compress_type=packed)
                        if key in logs:
                            entries.append((key, body))
                out.writestr(f"{root}/protokoll-{day}.txt",
                             a.summary_text(day, entries, sorted({k.rsplit("/", 1)[0] for k in keys
                                                                 if k.startswith(a.PREFIX["snapshots"])}),
                                            self.tz, 0, self.archive.now()),
                             compress_type=zipfile.ZIP_DEFLATED)
            if missing:
                out.writestr(f"{root}/fehlende-dateien.txt", "\n".join(missing) + "\n")

    async def file(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        if not a.valid_key(key):
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
