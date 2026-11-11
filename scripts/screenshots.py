"""The Page check, and the screenshots in docs/screenshots: starts the built
image in a throwaway container with made-up data (scripts/demo.py), opens
the page with a headless Chromium as Home Assistant's ingress would, and
checks every page in light, dark and at phone width (STANDARD.md §7 and §8):

- the header: the app's name (a back arrow on a subpage), then the stamp
  "As of HH:MM:SS", the refresh button (it reloads the page) and the link to
  the app's options with target="_top" and the slug from the host name;
- the version line "R2 Alarm Archive <version of config.yaml>" at the bottom;
- no console errors, no script errors, no Content-Security-Policy violations;
- dark mode, and no page wider than a phone at 390 px.

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

# (screenshot, path, back arrow); the snapshot's path is found on the day's page
PAGES = [("menu", "/", False), ("overview", "/overview", True), ("details", "/details", True),
         ("alarm-filter", "/details?alarms=1", True), ("day", "/day/2026-11-07", True),
         ("snapshot", None, True), ("queue", "/queue", True)]
# (suffix, width, colour scheme); the screenshots keep their names from before
MODES = [("", 1200, "light"), ("-dark", 1200, "dark"), ("-phone", 390, "light")]
SHOTS = {"menu", "overview", "details", "alarm-filter", "day", "snapshot", "queue", "overview-dark", "day-phone"}

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


def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


class Failures(list):
    def expect(self, ok: bool, where: str, what: str) -> None:
        if not ok:
            self.append(f"{where}: {what}")


def check_header(info: dict, where: str, back: bool, phone: bool, failed: Failures) -> None:
    start = [s for s in info["start"] if s != "div.actions"]
    failed.expect(start == (["a.icon-button.back", "h1"] if back else ["h1"]), where, f"header starts with {start}")
    failed.expect(info["start"][-1:] == ["div.actions"], where, "the actions are not last in the header")
    if back:
        failed.expect((info["back"] or "").startswith(INGRESS + "/"), where, f"back arrow to {info['back']!r}")
        failed.expect(bool(info["h1"]), where, "no title")
    else:
        failed.expect(info["h1"] == NAME, where, f"title {info['h1']!r}, not the app's name")
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
        context = await browser.new_context(viewport={"width": width, "height": 600}, color_scheme=scheme,
                                            device_scale_factor=1)
        await context.add_init_script(WATCH)
        await context.route(entry + "/**", ingress)
        await context.route(entry, ingress)
        page = await context.new_page()
        errors: list[str] = []
        page.on("console", lambda m, errors=errors: m.type == "error" and errors.append(m.text))
        page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
        for name, _, back in PAGES:
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
            check_header(info, where, back, width < 600, failed)
            failed.expect(dark(info["background"]) == (scheme == "dark"), where,
                          f"background {info['background']} in {scheme} mode")
            if name == "menu":
                # Refresh loads the same page again
                async with page.expect_navigation() as reload:
                    await page.click("#refresh")
                answer = await reload.value
                failed.expect(answer is not None and answer.status == 200 and page.url == entry + "/",
                              where, f"refresh went to {page.url}")
            failed.expect(not errors, where, f"console errors {errors}")
            if out is not None and where in SHOTS:
                await page.screenshot(path=out / f"{where}.png", full_page=True)
                print(f"{where}.png")
        await context.close()
    return failed


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--image", default="r2-alarm-archive:page", help="the built image to start")
    parser.add_argument("--out", type=Path, help="write the screenshots into this folder")
    args = parser.parse_args()
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as play:
        browser = await play.chromium.launch()
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
