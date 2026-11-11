"""The Page check, and the screenshots in docs/screenshots: starts the built
image in a throwaway container with made-up data (scripts/demo.py), opens
the page with a headless Chromium as Home Assistant's ingress would, and
checks every page in light, dark and at phone width (STANDARD.md §7 and §8):

- the header: the app's icon (loaded, from the page itself) and name with
  the three tabs in the same row, the active one the page's (on a subpage a
  back arrow and the title instead), then the stamp "As of HH:MM:SS", the
  refresh button (it reloads the page) and the link to the app's options
  with target="_top" and the slug from the host name; at phone width the
  tabs are a bar at the bottom of the window; the start page is the first
  tab, and a click on a tab puts it into the address, back returns;
- the version line "R2 Alarm Archive <version of config.yaml>" at the bottom;
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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
NAME = "R2 Alarm Archive"
VERSION = (ROOT / "r2_alarm_archive/config.yaml").read_text().split('version: "', 1)[1].split('"', 1)[0]
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
PAGES = [("start", "/", "overview"), ("overview", "/overview", "overview"), ("details", "/details", "details"),
         ("alarm-filter", "/details?alarms=1", "details"), ("day", "/day/2026-11-07", None),
         ("snapshot", None, None), ("queue", "/queue", "queue")]
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
  return {
    start: kids.map((e) => e.tagName.toLowerCase() + (e.className ? "." + e.className.replace(/ /g, ".") : "")),
    icon: (() => {
      const img = header && header.querySelector(":scope > img.app-icon");
      return img ? { src: img.getAttribute("src"), loaded: img.complete && img.naturalWidth > 0,
                     width: img.getBoundingClientRect().width } : null;
    })(),
    rows: (() => {
      // Vertical centres of the name and the tabs, and where the tab bar is
      const box = (el) => el ? el.getBoundingClientRect() : null;
      const h1 = box(header && header.querySelector(":scope > h1"));
      const nav = box(header && header.querySelector(":scope > nav.tabs"));
      const head = box(header);
      return { h1: h1 && (h1.top + h1.bottom) / 2, nav: nav && (nav.top + nav.bottom) / 2,
               navTop: nav && nav.top, navBottom: nav && nav.bottom, headBottom: head && head.bottom,
               window: window.innerHeight, bodyPad: parseFloat(getComputedStyle(document.body).paddingBottom) };
    })(),
    tabs: header ? [...header.querySelectorAll(":scope > nav.tabs > a")].map((a) => ({
      cls: a.className, href: a.getAttribute("href"), text: a.textContent, visible: a.offsetParent !== null,
      current: a.getAttribute("aria-current"),
    })) : [],
    h1: header && header.querySelector(":scope > h1") ? header.querySelector(":scope > h1").textContent : null,
    back: header && header.querySelector(":scope > a.back") ? header.querySelector(":scope > a.back").getAttribute("href") : null,
    actions: actions ? [...actions.children].map((e) => ({
      tag: e.tagName.toLowerCase(), cls: e.className, id: e.id, href: e.getAttribute("href"),
      target: e.getAttribute("target"), role: e.getAttribute("role"), title: e.getAttribute("title"),
      text: e.textContent, visible: e.offsetParent !== null, svg: !!e.querySelector("svg path[d]"),
    })) : [],
    footer: last && last.matches("footer.version") ? last.textContent : null,
    csp: window.__csp,
    wide: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    background: getComputedStyle(document.body).backgroundColor,
  };
}"""

# Home Assistant's frontend as far as the page reads it: the user's language,
# with the app in an iframe as the panel shows it (STANDARD.md §7)
OUTER = """<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title><link rel="icon" href="data:,">
<style>html, body { margin: 0; height: 100%; } iframe { border: 0; width: 100%; height: 100%; display: block; }</style>
</head><body><home-assistant></home-assistant><script>document.querySelector("home-assistant").hass =
{ language: "LANG", locale: { language: "LANG" }, themes: { darkMode: false } };</script>
<iframe src="INGRESS/" title="App"></iframe></body></html>"""
# The header in each language, as the language check reads it
LANG_TEXT = {"en": ("As of ", "Refresh", "Settings"), "de": ("Stand ", "Aktualisieren", "Einstellungen")}
LANG_INFO = """() => [document.documentElement.lang, getComputedStyle(document.documentElement).visibility,
  (document.querySelector("header .stamp") || {}).textContent, document.querySelector("#refresh").title,
  document.querySelector("header .actions a[target=_top]").title]"""


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


class Failures(list):
    def expect(self, ok: bool, where: str, what: str) -> None:
        if not ok:
            self.append(f"{where}: {what}")


def check_header(info: dict, where: str, tab: str | None, phone: bool, failed: Failures) -> None:
    start = [s for s in info["start"] if s != "div.actions"]
    expected = ["img.app-icon", "h1", "nav.tabs"] if tab else ["a.icon-button.back", "h1"]
    failed.expect(start == expected, where, f"header starts with {start}")
    failed.expect(info["start"][-1:] == ["div.actions"], where, "the actions are not last in the header")
    if tab is None:
        failed.expect((info["back"] or "").startswith(INGRESS + "/"), where, f"back arrow to {info['back']!r}")
        failed.expect(bool(info["h1"]), where, "no title")
    else:
        failed.expect(info["h1"] == NAME, where, f"title {info['h1']!r}, not the app's name")
        icon = info["icon"] or {}
        failed.expect(icon.get("src") == INGRESS + "/icon.png" and icon.get("loaded") and icon.get("width") == 24,
                      where, f"app icon {icon}")
        tabs = info["tabs"]
        failed.expect([(t["href"], t["text"]) for t in tabs] == [(f"{INGRESS}/{n}", label) for n, label in TABS],
                      where, f"tabs {[(t['href'], t['text']) for t in tabs]}")
        active = [t["href"].rsplit("/", 1)[1] for t in tabs if t["cls"] == "tab active" and t["current"] == "page"]
        failed.expect(active == [tab] and sum("active" in t["cls"] for t in tabs) == 1, where,
                      f"active tab {active}, not {tab}")
        failed.expect(all(t["visible"] for t in tabs), where, "a tab is hidden")
        rows = info["rows"]
        if phone:
            # A bar at the bottom of the window; the page keeps room for it
            failed.expect(rows["navBottom"] is not None and abs(rows["navBottom"] - rows["window"]) <= 1
                          and rows["bodyPad"] >= 56, where, f"tabs not at the bottom: {rows}")
        else:
            # In the row of the name: centred like it, inside the header
            failed.expect(rows["h1"] is not None and rows["nav"] is not None and abs(rows["h1"] - rows["nav"]) <= 2
                          and rows["navTop"] >= 0 and rows["navBottom"] <= rows["headBottom"] + 0.5,
                          where, f"tabs not in the row of the name: {rows}")
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
            info = await page.evaluate(HEADER)
            check_header(info, where, tab, width < 600, failed)
            failed.expect(dark(info["background"]) == (scheme == "dark"), where,
                          f"background {info['background']} in {scheme} mode")
            if name == "start":
                # Refresh loads the same page again
                async with page.expect_navigation() as reload:
                    await page.click("#refresh")
                answer = await reload.value
                failed.expect(answer is not None and answer.status == 200 and page.url == entry + "/",
                              where, f"refresh went to {page.url}")
                # A tab goes to its own address, which keeps it on reload; back returns
                await page.click('header nav.tabs a[href$="/queue"]')
                await page.wait_for_load_state()
                current = await page.get_attribute("header nav.tabs a.active", "href")
                failed.expect(page.url == entry + "/queue" and current == INGRESS + "/queue", where,
                              f"the queue tab went to {page.url}, active {current}")
                await page.reload()
                current = await page.get_attribute("header nav.tabs a.active", "href")
                failed.expect(current == INGRESS + "/queue", where, f"after a reload the active tab is {current}")
                await page.go_back()
                failed.expect(page.url == entry + "/", where, f"back went to {page.url}")
            failed.expect(not errors, where, f"console errors {errors}")
            if out is not None and where in SHOTS:
                # At phone width the window as a phone shows it: the tab bar at its bottom
                await page.screenshot(path=out / f"{where}.png", full_page=width >= 600)
                print(f"{where}.png")
        await context.close()
    await languages(browser, base, entry, failed)
    return failed


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
        lang = request.url.partition("?lang=")[2]
        await route.fulfill(content_type="text/html", body=OUTER.replace("LANG", lang).replace("INGRESS", INGRESS))

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
        shown, visible, stamp, refresh, gear = await app.evaluate(LANG_INFO)
        failed.expect([shown, visible] == [lang, "visible"], where, f"page in {shown}, {visible}, not {lang}")
        failed.expect(len(loads) == expected, where, f"loaded {len(loads)} time(s), not {expected}")
        failed.expect((stamp or "").startswith(LANG_TEXT[lang][0]) and [refresh, gear] == list(LANG_TEXT[lang][1:]),
                      where, f"header {stamp!r}, {refresh!r}, {gear!r}")
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
    print(f"Page check passed: {pages} pages (light, dark, phone), version {VERSION}, slug {SLUG}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
