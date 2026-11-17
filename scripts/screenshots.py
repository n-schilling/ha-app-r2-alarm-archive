"""The Page check, and the screenshots in docs/screenshots: starts the built
image in a throwaway container with made-up data (scripts/demo.py), opens
the page with a headless Chromium as Home Assistant's ingress would, and
checks every page in light, dark and at phone width (STANDARD.md §7 and §8):

- the header as Home Assistant's own pages draw it (hass-tabs-subpage): the
  menu button first (shown only when narrow, below 870 px), the h1 with the
  name (`name` in config.yaml), hidden beside the tabs like the icon, and the
  three tabs, the active one the page's (on a subpage a back arrow and the
  title instead, no menu), then the stamp "As of HH:MM:SS" (hidden when
  narrow), the refresh button (it reloads the page and moves the stamp) and
  the link to the app's options with target="_top" and the slug from the
  host name; on a wide screen the tabs centred in the header's row, icon
  beside label and a line under the active one, no bar at the bottom; when
  narrow the tabs are a bar at the bottom of the window, icon over label;
  menu and buttons stay inside the header; the start page is the first view,
  a click on a tab puts `?tab=<view>` into the address, back returns, and
  the old addresses (/details?month=…) lead to `?tab=`;
- Home Assistant's frame (the stand-in around the ingress frame plays its
  part, share/ha-app/ha-frame.js the page's): no kiosk mode on a wide
  screen; on a phone (390 x 844, with a notch of 47 px and a home bar of
  34 px, and without) kiosk mode, so no bar of Home Assistant above the
  app, the insets handled by the page: the header from the top, 56 px plus
  the notch, the bar at the bottom of the window with the home bar's
  padding, every tab on the screen; the menu button opens Home Assistant's
  sidebar; wider and back switches kiosk mode off and on with one message
  each way;
- the version line "<name> <version of config.yaml>" at the bottom;
- no console errors, no script errors, no Content-Security-Policy violations;
- dark mode, and no page wider than a phone at 390 px;
- the page takes the language of Home Assistant's interface, not the
  browser's (ha-lang.js; a stand-in for its frontend in German, English and
  French around the ingress frame): one reload when it changes, none when
  not, and the cookie ha_lang for the ingress path only.

"As ingress would": the container sits in a network whose gateway has the
Supervisor's address 172.30.32.2, so the page's own check of the client
address stays on; every request goes to /api/hassio_ingress/<token>/… and
reaches the page without that prefix and with the X-Ingress-Path header,
as the Supervisor's proxy hands it on. Nothing here talks to a real
Home Assistant.

    docker build --build-arg BUILD_VERSION="$(yq -r .version r2_alarm_archive/config.yaml)" \\
        -t r2-alarm-archive:page r2_alarm_archive
    python3 -m venv .venv-shots
    .venv-shots/bin/pip install playwright==1.64.1
    .venv-shots/bin/playwright install chromium
    .venv-shots/bin/python scripts/screenshots.py                          # check only
    .venv-shots/bin/python scripts/screenshots.py --out docs/screenshots   # and draw the screenshots
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "r2_alarm_archive/config.yaml").read_text()
# The name the app store shows, which the header must show as it is
NAME = re.search(r'^name: "?(.*?)"?$', CONFIG, re.M).group(1)
VERSION = CONFIG.split('version: "', 1)[1].split('"', 1)[0]
# Like a container of the Supervisor: the host name gives the slug
HOSTNAME = "1a2b3c4d-r2-alarm-archive"
SLUG = HOSTNAME.replace("-", "_")
SUPERVISOR = "172.30.32.2"
INGRESS = "/api/hassio_ingress/Zm9yLXRoZS1wYWdlLWNoZWNr"

CAMERAS = {"camera.front_door": ("Front door", "#7fa7d9"), "camera.garden": ("Garden", "#7fa37a")}
PICTURE = """<!doctype html><body style="margin:0"><div style="width:640px;height:360px;position:relative;
font:20px sans-serif;color:#fff;background:linear-gradient(135deg,{color},#e8eef0)">
<svg viewBox="0 0 24 24" style="position:absolute;left:250px;top:110px;width:140px;fill:rgba(255,255,255,.6)">
<path d="M4,4H7L9,2H15L17,4H20A2,2 0 0,1 22,6V18A2,2 0 0,1 20,20H4A2,2 0 0,1 2,18V6A2,2 0 0,1 4,4M12,7A5,5 0 0,0
7,12A5,5 0 0,0 12,17A5,5 0 0,0 17,12A5,5 0 0,0 12,7M12,9A3,3 0 0,1 15,12A3,3 0 0,1 12,15A3,3 0 0,1 9,12A3,3 0 0,1
12,9Z"/></svg><span style="position:absolute;left:16px;bottom:12px">{name} · demo picture</span></div></body>"""

# (screenshot, path, active tab or None for a subpage with a back arrow); the
# snapshot's path is found on the day's page
PAGES = [("start", "/", "overview"), ("overview", "/?tab=overview", "overview"),
         ("details", "/?tab=details", "details"), ("alarm-filter", "/?tab=details&alarms=1", "details"),
         ("day", "/day/2026-11-07", None), ("snapshot", None, None), ("queue", "/?tab=queue", "queue")]
TABS = [("overview", "Overview"), ("details", "Details"), ("queue", "Queue")]
# (suffix, width, colour scheme); the screenshots keep their names from before
MODES = [("", 1200, "light"), ("-dark", 1200, "dark"), ("-phone", 390, "light")]
SHOTS = {"overview", "overview-phone", "details", "alarm-filter", "day", "snapshot", "queue", "overview-dark", "day-phone"}

# Collects Content-Security-Policy violations before the page's own script runs
WATCH = """window.__csp = [];
document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(e.violatedDirective + " " + e.blockedURI));"""
HEADER = """() => {
  const header = document.querySelector("body > header");
  const kids = header ? [...header.children] : [];
  const actions = header && header.querySelector(":scope > .actions");
  const last = document.body.lastElementChild;
  const box = (el) => el.getBoundingClientRect(), head = header && box(header);
  const shown = (el) => !!el && getComputedStyle(el).display !== "none" && box(el).width > 1;
  const inside = (el) => { const r = box(el);
    return r.left >= head.left - 0.5 && r.right <= head.right + 0.5 && r.top >= head.top - 0.5 && r.bottom <= head.bottom + 0.5; };
  const what = (e) => e.tagName.toLowerCase() + (e.className ? "." + e.className.replace(/ /g, ".") : "");
  const menu = header && header.querySelector(":scope > .menu"), h1 = header && header.querySelector(":scope > h1");
  const nav = header && header.querySelector(":scope > nav.tabs");
  const list = nav ? [...nav.querySelectorAll(":scope > a")] : [];
  const after = (el) => getComputedStyle(el, "::after").content;
  return {
    start: kids.map(what),
    narrow: document.documentElement.classList.contains("narrow"),
    top: head && head.top, height: head && head.height,
    brand: !!header && [...header.querySelectorAll("img")].some(shown),
    h1Hidden: !!h1 && box(h1).width <= 1 && box(h1).height <= 1,
    menu: menu && { first: kids[0] === menu, ha: menu.hasAttribute("data-ha-menu"), role: menu.getAttribute("role"),
                    title: menu.getAttribute("title"), label: menu.getAttribute("aria-label"),
                    icon: !!menu.querySelector("svg path[d]"), shown: shown(menu) },
    // Where the tabs are: in the header's row, centred between its start and the actions (wide),
    // or a bar at the bottom of the window, clear of the home bar (narrow)
    nav: nav && (() => {
      const r = box(nav), first = box(list[0]), lastTab = box(list[list.length - 1]);
      const pad = parseFloat(getComputedStyle(nav).paddingBottom);
      return { fixed: getComputedStyle(nav).position === "fixed", bottom: r.bottom - innerHeight,
               pad: getComputedStyle(nav).paddingBottom, bodyPad: parseFloat(getComputedStyle(document.body).paddingBottom),
               row: list.every(inside), centred: Math.abs((first.left + lastTab.right) / 2 - (r.left + r.right) / 2) < 1
                    && r.right <= box(actions).left + 0.5,
               onScreen: list.every((t) => { const q = box(t);
                 return q.left >= 0 && q.right <= innerWidth && q.bottom <= innerHeight - pad + 0.5; }),
               icons: list.every((t) => shown(t.querySelector("svg"))) };
    })(),
    // Menu (or back arrow), the subpage's title and the visible buttons, whether each lies in the header's box
    outside: header ? [menu, ...header.querySelectorAll(":scope > a.back, :scope > .actions > *")]
      .filter(shown).concat(nav ? [] : [h1].filter(Boolean)).filter((e) => !inside(e)).map(what) : [],
    tabs: list.map((a) => ({
      cls: a.className, href: a.getAttribute("href"), text: a.textContent, visible: a.offsetParent !== null,
      current: a.getAttribute("aria-current"), line: after(a) !== "none" && after(a) !== "",
    })),
    h1: header && header.querySelector(":scope > h1") ? header.querySelector(":scope > h1").textContent : null,
    back: header && header.querySelector(":scope > a.back") ? header.querySelector(":scope > a.back").getAttribute("href") : null,
    actions: actions ? [...actions.children].map((e) => ({
      tag: e.tagName.toLowerCase(), cls: e.className, id: e.id, href: e.getAttribute("href"),
      target: e.getAttribute("target"), role: e.getAttribute("role"), title: e.getAttribute("title"),
      text: e.textContent, visible: e.offsetParent !== null && getComputedStyle(e).display !== "none",
      svg: !!e.querySelector("svg path[d]"),
    })) : [],
    footer: last && last.matches("footer.version") ? last.textContent : null,
    csp: window.__csp,
    wide: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    background: getComputedStyle(document.body).backgroundColor,
  };
}"""

# The stand-in for Home Assistant around the ingress frame, served at
# /home-assistant (STANDARD.md §7):
#   ?lang=de      with a stand-in for the frontend's <home-assistant> element,
#                 whose hass names that language (and light mode), as the
#                 page reads it (share/ha-app/ha-lang.js)
#   ?safe=47,34   on a phone with a notch: the safe-area insets top and
#                 bottom in px that Home Assistant passes on
# It plays Home Assistant's part (its app panel, ha-panel-app) for the page's
# share/ha-app/ha-frame.js: it answers subscribe-properties with properties
# (narrow below 870 px, the insets, the route) and again on every resize; it
# shows its 40 px bar #ha-bar above the frame while narrow and not in kiosk
# mode, and pads the frame by the insets unless the page handles them
# (handleSafeArea). What the page asked for lands in window.__kiosk,
# window.__safe and window.__menu (toggle-menu clicks), every message in
# window.__messages.
OUTER = """<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title><link rel="icon" href="data:,">
<style>html, body { margin: 0; height: 100%; }
body { display: flex; flex-direction: column; }
#ha-bar { flex: none; height: 40px; background: #888; }
#ha-app { flex: 1; min-height: 0; box-sizing: border-box; }
iframe { border: 0; width: 100%; height: 100%; display: block; }</style>
<script>FRAME</script></head><body>HASS<div id="ha-bar" hidden></div>
<div id="ha-app"><iframe id="ingress" src="INGRESS/" title="App"></iframe></div></body></html>"""
# Home Assistant's app panel as far as ha-frame.js talks to it; in the head, so
# it listens before the frame loads
FRAME = """(() => {
  const safe = (new URLSearchParams(location.search).get("safe") || "0,0").split(",");
  const inset = (n) => (parseInt(n, 10) || 0) + "px";
  const insets = { top: inset(safe[0]), right: "0px", bottom: inset(safe[1]), left: "0px" };
  const frame = { get contentWindow() { return document.getElementById("ingress").contentWindow; } };
  Object.assign(window, { __kiosk: null, __safe: null, __menu: 0, __messages: [] });
  let subscribed = false;
  const narrow = () => matchMedia("(max-width: 870px)").matches;
  const layout = () => {
    const bar = document.getElementById("ha-bar"), box = document.getElementById("ha-app");
    if (!bar || !box) return;
    bar.hidden = !(narrow() && !window.__kiosk);
    box.style.paddingTop = window.__safe || !bar.hidden ? "0px" : insets.top;
    box.style.paddingBottom = window.__safe ? "0px" : insets.bottom;
  };
  const send = () => {
    if (subscribed) frame.contentWindow.postMessage({ type: "home-assistant/properties", narrow: narrow(),
      route: { prefix: "/app/demo", path: "" }, safeAreaInsets: insets }, location.origin);
  };
  window.addEventListener("message", (e) => {
    if (e.source !== frame.contentWindow || !e.data) return;
    window.__messages.push(e.data);
    if (e.data.type === "home-assistant/subscribe-properties") {
      subscribed = true;
      window.__kiosk = !!e.data.kioskMode;
      window.__safe = !!e.data.handleSafeArea;
      layout();
      send();
    } else if (e.data.type === "home-assistant/unsubscribe-properties") {
      subscribed = false;
      window.__kiosk = false;
      window.__safe = false;
      layout();
    } else if (e.data.type === "home-assistant/toggle-menu") {
      window.__menu += 1;
    }
  });
  window.addEventListener("resize", () => { layout(); send(); });
  document.addEventListener("DOMContentLoaded", layout);
})();"""
# Home Assistant's frontend as far as the page reads it: the user's language
HASS = """<home-assistant></home-assistant><script>document.querySelector("home-assistant").hass =
{ language: "LANG", locale: { language: "LANG" }, themes: { darkMode: false } };</script>"""
LANG_CODE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
# The header in each language, as the language check reads it
LANG_TEXT = {"en": ("As of ", "Refresh", "Settings", "Menu"), "de": ("Stand ", "Aktualisieren", "Einstellungen", "Menü")}
LANG_INFO = """() => [document.documentElement.lang, getComputedStyle(document.documentElement).visibility,
  (document.querySelector("header .stamp") || {}).textContent, document.querySelector("#refresh").title,
  document.querySelector("header .actions a[target=_top]").title, document.querySelector("header .menu").title]"""


def outer_page(url: str) -> str:
    """The stand-in for Home Assistant around the ingress frame (see OUTER)."""
    lang = (parse_qs(urlsplit(url).query).get("lang") or [""])[0]
    hass = HASS.replace("LANG", lang) if LANG_CODE.fullmatch(lang) else ""
    return OUTER.replace("FRAME", FRAME).replace("HASS", hass).replace("INGRESS", INGRESS)


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


class Failures(list):
    def expect(self, ok: bool, where: str, what: str) -> None:
        if not ok:
            self.append(f"{where}: {what}")


def check_header(info: dict, where: str, tab: str | None, phone: bool, failed: Failures,
                 safe: tuple[int, int] = (0, 0)) -> None:
    """The header as Home Assistant's own pages draw it (hass-tabs-subpage, STANDARD.md §7):
    phone is Home Assistant's narrow (below 870 px), safe the insets top and bottom."""
    start = [s for s in info["start"] if s != "div.actions"]
    expected = ["a.icon-button.menu", "h1.name", "nav.tabs"] if tab else ["a.icon-button.back", "h1"]
    failed.expect(start == expected, where, f"header starts with {start}")
    failed.expect(info["start"][-1:] == ["div.actions"], where, "the actions are not last in the header")
    failed.expect(info["narrow"] == phone, where, f"html.narrow is {info['narrow']}, not {phone}")
    failed.expect(abs(info["top"]) < 0.5 and abs(info["height"] - 56 - safe[0]) < 1, where,
                  f"header at {info['top']}, {info['height']} px high, not from the top and 56 + {safe[0]} px")
    if tab is None:
        failed.expect((info["back"] or "").startswith(INGRESS + "/"), where, f"back arrow to {info['back']!r}")
        failed.expect(bool(info["h1"]), where, "no title")
        failed.expect(info["menu"] is None and not info["tabs"], where, "a menu button or tabs on a subpage")
    else:
        failed.expect(info["h1"] == NAME, where, f"title {info['h1']!r}, not the app's name")
        failed.expect(info["h1Hidden"] and not info["brand"], where, "with tabs an icon or a visible name")
        m = info["menu"] or {}
        failed.expect(m.get("first") and m.get("ha") and m.get("role") == "button" and m.get("title") == "Menu"
                      and m.get("label") == "Menu" and m.get("icon"), where, f"menu button {m}")
        failed.expect(m.get("shown") == phone, where, "the menu button is " + ("hidden" if phone else "shown"))
        tabs = info["tabs"]
        failed.expect([(t["href"], t["text"]) for t in tabs] == [(f"?tab={n}", label) for n, label in TABS],
                      where, f"tabs {[(t['href'], t['text']) for t in tabs]}")
        active = [t["href"].split("=", 1)[1] for t in tabs if t["cls"] == "tab active" and t["current"] == "page"]
        failed.expect(active == [tab] and sum("active" in t["cls"] for t in tabs) == 1, where,
                      f"active tab {active}, not {tab}")
        failed.expect(all(t["visible"] for t in tabs), where, "a tab is hidden")
        nav = info["nav"] or {}
        failed.expect(bool(nav.get("icons")), where, "a tab without its icon")
        if phone:
            # A bar at the bottom of the window, clear of the home bar; the page keeps room for it
            failed.expect(nav.get("fixed") and abs(nav.get("bottom", 1)) < 0.5 and nav.get("pad") == f"{safe[1]}px"
                          and nav.get("onScreen") and nav.get("bodyPad", 0) >= 56 + safe[1]
                          and not any(t["line"] for t in tabs),
                          where, f"tabs not a bar at the bottom, {safe[1]} px clear of the home bar: {nav}")
        else:
            failed.expect(not nav.get("fixed") and nav.get("row") and nav.get("centred") and nav.get("bodyPad", 99) < 56
                          and [t["line"] for t in tabs] == [t["href"] == f"?tab={tab}" for t in tabs],
                          where, f"tabs not centred in the header's row with a line under the active one: {nav}")
    failed.expect(not info["outside"], where, f"outside the header: {info['outside']}")
    acts = info["actions"]
    failed.expect(len(acts) == 3, where, f"{len(acts)} items in .actions, not stamp, refresh and settings")
    if len(acts) == 3:
        stamp, refresh, gear = acts
        failed.expect(stamp["tag"] == "span" and stamp["cls"] == "stamp opt"
                      and re.fullmatch(r"As of \d\d:\d\d:\d\d", stamp["text"]) is not None,
                      where, f"stamp {stamp}")
        failed.expect(stamp["visible"] != phone, where, "the stamp is " + ("shown" if phone else "hidden"))
        failed.expect(refresh["tag"] == "a" and refresh["id"] == "refresh" and refresh["href"] == ""
                      and refresh["role"] == "button" and refresh["title"] == "Refresh" and refresh["svg"],
                      where, f"refresh {refresh}")
        failed.expect(gear["tag"] == "a" and gear["href"] == f"/config/app/{SLUG}/config"
                      and gear["target"] == "_top" and gear["title"] == "Settings" and gear["svg"],
                      where, f"settings {gear}")
    failed.expect(info["footer"] == f"{NAME} {VERSION}", where, f"version line {info['footer']!r}")
    failed.expect(not info["csp"], where, f"CSP violations {info['csp']}")
    failed.expect(info["wide"] <= 0, where, f"{info['wide']} px wider than the window")


async def security_headers(answer, where: str, failed: Failures) -> None:
    """The headers STANDARD.md §3 asks of every answer of the page."""
    # A page's answer (Response) has all of them in all_headers(); context.request's (APIResponse) in headers
    headers = {} if answer is None else await answer.all_headers() if hasattr(answer, "all_headers") \
        else answer.headers
    policy = {
        part.split()[0]: part.split()[1:]
        for part in headers.get("content-security-policy", "").split(";") if part.strip()
    }
    scripts = policy.get("script-src", [])
    failed.expect(policy.get("default-src") == ["'none'"], where, f"CSP default-src {policy.get('default-src')}")
    failed.expect("'unsafe-inline'" not in scripts and any(s.startswith(("'sha256-", "'nonce-")) for s in scripts),
                  where, f"CSP script-src {scripts}, not by hash or nonce")
    failed.expect(policy.get("frame-ancestors") == ["'self'"], where, "CSP frame-ancestors not 'self'")
    failed.expect(policy.get("base-uri") == ["'none'"], where, "CSP base-uri not 'none'")
    failed.expect(policy.get("form-action") in (["'none'"], ["'self'"]), where, "CSP form-action")
    failed.expect(headers.get("x-content-type-options") == "nosniff", where, "X-Content-Type-Options not nosniff")
    failed.expect(headers.get("referrer-policy") in ("no-referrer", "same-origin"), where,
                  f"Referrer-Policy {headers.get('referrer-policy')}")
    failed.expect(headers.get("cache-control") == "no-store", where, f"Cache-Control {headers.get('cache-control')}")


def dark(colour: str) -> bool:
    r, g, b = (int(n) for n in re.findall(r"\d+", colour)[:3])
    return r + g + b < 3 * 96


def start_container(image: str, pictures: Path) -> tuple[str, str, str]:
    network = f"r2-page-{os.getpid()}"
    # One such network at a time on a machine: wait a little for another check to end
    for attempt in range(30):
        made = subprocess.run(["docker", "network", "create", "--subnet", "172.30.32.0/23", "--gateway",
                               SUPERVISOR, network], capture_output=True, text=True)
        if made.returncode == 0:
            break
        if attempt == 29:
            raise SystemExit(f"No network with the Supervisor's address: {made.stderr.strip()}")
        time.sleep(2)
    try:
        container = docker(
            "run", "-d", "--network", network, "--hostname", HOSTNAME, "--user", "r2archive",
            "-p", "127.0.0.1::8099", "-e", "PYTHONPATH=/usr/lib",
            "-v", f"{ROOT / 'scripts'}:/demo/scripts:ro", "-v", f"{ROOT / 'tests'}:/demo/tests:ro",
            "-v", f"{pictures}:/demo/pictures:ro",
            "--entrypoint", "python3", image, "/demo/scripts/demo.py", "/demo/pictures")
    except subprocess.CalledProcessError:
        docker("network", "rm", network, check=False)
        raise
    port = docker("port", container, "8099").splitlines()[0].rsplit(":", 1)[1]
    return network, container, f"http://127.0.0.1:{port}"


def wait_for(base: str, container: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as answer:
                if answer.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline or not docker("ps", "-q", "--filter", f"id={container}"):
            raise SystemExit("The page did not come up:\n" + docker("logs", container, check=False))
        time.sleep(0.5)


async def pictures(browser, folder: Path) -> None:
    page = await browser.new_page(viewport={"width": 640, "height": 360})
    for camera, (name, color) in CAMERAS.items():
        await page.set_content(PICTURE.format(color=color, name=name))
        await page.screenshot(path=folder / f"{camera.split('.', 1)[1]}.jpg", type="jpeg", quality=80)
        (folder / f"{camera.split('.', 1)[1]}.jpg").chmod(0o644)
    await page.close()


async def check(browser, base: str, out: Path | None) -> Failures:
    failed = Failures()
    entry = base + INGRESS

    async def ingress(route, request):
        # The Supervisor's proxy: the prefix off, the header on
        headers = {**request.headers, "x-ingress-path": INGRESS}
        await route.continue_(url=base + request.url[len(entry):], headers=headers)

    paths = {name: path for name, path, _ in PAGES}
    for suffix, width, scheme in MODES:
        context = await browser.new_context(viewport={"width": width, "height": 600 if width >= 600 else 844}, color_scheme=scheme,
                                            device_scale_factor=1)
        await context.add_init_script(WATCH)
        await context.route(entry + "/**", ingress)
        await context.route(entry, ingress)
        page = await context.new_page()
        errors: list[str] = []
        page.on("console", lambda m, errors=errors: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
        for name, _, tab in PAGES:
            where = f"{name}{suffix}"
            if paths[name] is None:
                await page.goto(entry + paths["day"])
                href = await page.get_attribute('a.row[href*="/snapshot/"][href*="_triggered"]', "href")
                paths[name] = href[len(INGRESS):]
            errors.clear()
            answer = await page.goto(entry + paths[name])
            failed.expect(answer is not None and answer.status == 200, where,
                          f"HTTP {answer.status if answer else '–'}")
            await security_headers(answer, where, failed)
            info = await page.evaluate(HEADER)
            check_header(info, where, tab, width <= 870, failed)
            failed.expect(dark(info["background"]) == (scheme == "dark"), where,
                          f"background {info['background']} in {scheme} mode")
            if name == "day":
                failed.expect(info["back"] == f"{INGRESS}/?tab=details&month=2026-11", where,
                              f"back arrow to {info['back']!r}, not the details of its month")
            if name == "start":
                # Refresh loads the same page again and moves the stamp (the demo's clock runs)
                before = await page.text_content("header .stamp")
                await page.wait_for_timeout(1100)
                async with page.expect_navigation() as reload:
                    await page.click("#refresh")
                answer = await reload.value
                failed.expect(answer is not None and answer.status == 200 and page.url == entry + "/",
                              where, f"refresh went to {page.url}")
                after = await page.text_content("header .stamp")
                failed.expect(after != before, where, f"refresh left the stamp at {after!r}")
                # A tab puts its view into the address, which keeps it on reload; back returns
                await page.click('header nav.tabs a[href="?tab=queue"]')
                await page.wait_for_load_state()
                current = await page.get_attribute("header nav.tabs a.active", "href")
                failed.expect(page.url == entry + "/?tab=queue" and current == "?tab=queue", where,
                              f"the queue tab went to {page.url}, active {current}")
                await page.reload()
                current = await page.get_attribute("header nav.tabs a.active", "href")
                failed.expect(current == "?tab=queue", where, f"after a reload the active tab is {current}")
                await page.go_back()
                failed.expect(page.url == entry + "/", where, f"back went to {page.url}")
                # The old address of a view leads to its tab, with its query, under the
                # ingress path (asked here, as the browser does not route a redirect again)
                moved = await context.request.get(base + "/details?month=2026-10", max_redirects=0,
                                                  headers={"x-ingress-path": INGRESS})
                location = moved.headers.get("location")
                await security_headers(moved, where + " (303)", failed)
                failed.expect(moved.status == 303 and location == f"{INGRESS}/?tab=details&month=2026-10", where,
                              f"/details?month=2026-10 answered {moved.status} to {location}")
                answer = await page.goto(base + (location or ""))
                current = await page.get_attribute("header nav.tabs a.active", "href")
                failed.expect(answer is not None and answer.status == 200 and current == "?tab=details",
                              where, f"{location} answered {answer and answer.status}, active {current}")
                failed.expect(await page.get_attribute("#m-2026-10", "open") is not None, where,
                              "the old address did not keep its month")
            failed.expect(not errors, where, f"console errors {errors}")
            if out is not None and where in SHOTS:
                # At phone width the window as a phone shows it: the tab bar at its bottom
                await page.screenshot(path=out / f"{where}.png", full_page=width > 870)
                print(f"{where}.png")
        await context.close()
    await frames(browser, base, entry, failed)
    await languages(browser, base, entry, failed)
    return failed


async def frames(browser, base: str, entry: str, failed: Failures) -> None:
    """Home Assistant's part of the frame (OUTER plays it, ha-frame.js the page's):
    kiosk mode only while narrow, the notch and the home bar by Home Assistant's
    insets, the menu button; wide, on a phone with a notch and a home bar, and
    on a phone without."""
    async def ingress(route, request):
        headers = {**request.headers, "x-ingress-path": INGRESS}
        await route.continue_(url=base + request.url[len(entry):], headers=headers)

    async def outer(route, request):
        await route.fulfill(content_type="text/html", body=outer_page(request.url))

    seen_js = """() => ({kiosk: window.__kiosk, safe: window.__safe,
      messages: window.__messages.map((m) => m.type + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : "")),
      bar: getComputedStyle(document.getElementById("ha-bar")).display !== "none",
      frame: (() => { const r = document.getElementById("ingress").getBoundingClientRect(); return [r.top, r.bottom - innerHeight]; })()})"""
    sent_js = """() => window.__messages.filter((m) => m.type.endsWith("-properties"))
      .map((m) => m.type.split("/")[1] + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : ""))"""
    for width, height, safe in ((1200, 760, (0, 0)), (390, 844, (47, 34)), (390, 844, (0, 0))):
        phone = width <= 870
        where = f"frame, {width} px" + (f", notch {safe[0]} px, home bar {safe[1]} px" if safe != (0, 0) else "")
        context = await browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark",
                                            device_scale_factor=1)
        await context.add_init_script(WATCH)
        await context.route(entry + "/**", ingress)
        await context.route(base + "/home-assistant*", outer)
        page = await context.new_page()
        errors: list[str] = []
        page.on("console", lambda m, errors=errors: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
        await page.goto(f"{base}/home-assistant" + (f"?safe={safe[0]},{safe[1]}" if safe != (0, 0) else ""))
        await page.frame_locator("#ingress").locator("body > header").wait_for(state="attached")
        await page.wait_for_function("window.__safe !== null")
        await page.wait_for_timeout(500)  # room for the answer, and for any further message, which must not loop
        app = next(f for f in page.frames if f.parent_frame is not None)
        await app.wait_for_selector("html.narrow" if phone else "html:not(.narrow)", state="attached")
        if safe != (0, 0):
            await app.wait_for_function("getComputedStyle(document.documentElement)"
                                        f".getPropertyValue('--safe-area-inset-top').trim() === '{safe[0]}px'")
        seen = await page.evaluate(seen_js)
        failed.expect(seen["kiosk"] is phone, where, f"kiosk mode {seen['kiosk']}, not {phone} ({seen['messages']})")
        failed.expect(seen["safe"] is True, where, "the page does not keep clear of the notch itself (handleSafeArea)")
        failed.expect(not seen["bar"], where, "Home Assistant's bar above the app")
        failed.expect(seen["frame"] == [0, 0], where, f"the frame does not fill the window ({seen['frame']})")
        failed.expect(len(seen["messages"]) == 1, where, f"not one message ({seen['messages']})")
        check_header(await app.evaluate(HEADER), where, "overview", phone, failed, safe)
        if phone:
            await app.locator("body > header .menu").click()
            # The message arrives a moment later; room for a second one, which must not come
            with contextlib.suppress(PlaywrightError):
                await page.wait_for_function("window.__menu > 0", timeout=2000)
            await page.wait_for_timeout(200)
            failed.expect(await page.evaluate("window.__menu") == 1, where, "the menu button did not open the sidebar")
            if safe != (0, 0):
                # Wider than narrow and back: kiosk mode off (Home Assistant's bar back), then on again
                await page.set_viewport_size({"width": 1200, "height": height})
                await app.wait_for_selector("html:not(.narrow)", state="attached")
                await page.wait_for_timeout(300)
                failed.expect(await page.evaluate("window.__kiosk") is False, where, "wide again, kiosk mode still on")
                await page.set_viewport_size({"width": width, "height": height})
                await app.wait_for_selector("html.narrow", state="attached")
                await page.wait_for_timeout(300)
                sent = await page.evaluate(sent_js)
                failed.expect(await page.evaluate("window.__kiosk") is True and sent == [
                    "subscribe-properties:true", "unsubscribe-properties", "subscribe-properties:false",
                    "subscribe-properties:true"], where, f"narrow again: not kiosk mode, one message each way ({sent})")
                check_header(await app.evaluate(HEADER), where + ", narrow again", "overview", True, failed, safe)
        csp = await app.evaluate("window.__csp")
        failed.expect(not errors and not csp, where, f"console errors {errors}, CSP violations {csp}")
        await context.close()


async def languages(browser, base: str, entry: str, failed: Failures) -> None:
    """The browser asks for English, Home Assistant's configured language is
    English (demo.py); the page follows the viewer's language in Home Assistant
    (ha-lang.js): one reload when it changes, none when not, and none in a loop
    for a language the app does not have."""
    async def ingress(route, request):
        # With the cookie, which request.headers leaves out and Chromium drops
        # when it continues to another path: fetched here and handed back
        headers = {**await request.all_headers(), "x-ingress-path": INGRESS}
        await route.fulfill(response=await route.fetch(url=base + request.url[len(entry):], headers=headers))

    async def outer(route, request):
        await route.fulfill(content_type="text/html", body=outer_page(request.url))

    context = await browser.new_context(viewport={"width": 1040, "height": 800}, locale="en-US",
                                        extra_http_headers={"Accept-Language": "en"})
    await context.add_init_script(WATCH)
    await context.route(entry + "/**", ingress)
    await context.route(base + "/home-assistant?*", outer)
    page = await context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    loads: list[str] = []
    page.on("framenavigated", lambda f: f.parent_frame is not None and loads.append(f.url))
    for ha_lang, lang, expected in (("de", "de", 2), ("de", "de", 1), ("en-GB", "en", 2), ("fr", "en", 2),
                                    ("fr", "en", 1)):
        where = f"Home Assistant in {ha_lang}"
        loads.clear()
        await page.goto(f"{base}/home-assistant?lang={ha_lang}")
        await page.wait_for_timeout(1500)  # room for another reload, which must not come
        app = next(f for f in page.frames if f.parent_frame is not None)
        await app.locator("body > header h1").wait_for()
        shown, visible, stamp, refresh, gear, menu = await app.evaluate(LANG_INFO)
        failed.expect([shown, visible] == [lang, "visible"], where, f"page in {shown}, {visible}, not {lang}")
        failed.expect(len(loads) == expected, where, f"loaded {len(loads)} time(s), not {expected}")
        failed.expect((stamp or "").startswith(LANG_TEXT[lang][0]) and [refresh, gear, menu] == list(LANG_TEXT[lang][1:]),
                      where, f"header {stamp!r}, {refresh!r}, {gear!r}, {menu!r}")
        held = [(k["value"], k["path"]) for k in await context.cookies() if k["name"] == "ha_lang"]
        failed.expect(held == [(ha_lang.split("-")[0], f"{INGRESS}/")], where, f"cookie {held}")
    csp = await app.evaluate("window.__csp")
    failed.expect(not errors and not csp, "language", f"console errors {errors}, CSP violations {csp}")
    await context.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--image", default="r2-alarm-archive:page", help="the built image to start")
    parser.add_argument("--out", type=Path, help="write the screenshots into this folder")
    args = parser.parse_args()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as play:
        # The installed Google Chrome where there is one (the CI runner has it),
        # otherwise Playwright's Chromium
        chrome = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
        browser = await play.chromium.launch(channel="chrome" if chrome else None)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            folder.chmod(0o755)  # the container's user reads the pictures
            await pictures(browser, folder)
            network, container, base = start_container(args.image, folder)
            try:
                wait_for(base, container)
                failed = await check(browser, base, args.out)
            finally:
                logs = docker("logs", container, check=False)
                docker("rm", "-f", container, check=False)
                docker("network", "rm", network, check=False)
        await browser.close()
    pages = len(PAGES) * len(MODES)
    if failed:
        print(f"Page check failed ({len(failed)} of the checks on {pages} pages):", *failed, sep="\n  ")
        print("Container log:\n" + logs)
        return 1
    print(f"Page check passed: {pages} pages (light, dark, phone), Home Assistant's frame (wide, phone with"
          f" and without a notch), version {VERSION}, slug {SLUG}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
