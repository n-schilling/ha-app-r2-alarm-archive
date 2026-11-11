"""Takes the screenshots in docs/screenshots: the web page with made-up data,
served on 127.0.0.1 from the app's own code with the test fakes in place of
Home Assistant, R2 and Cloudflare, and drawn by a headless Chromium.

    python3 -m venv .venv-shots
    .venv-shots/bin/pip install --require-hashes -r r2_alarm_archive/requirements.txt
    .venv-shots/bin/pip install -r requirements_test.txt playwright==1.64.1
    .venv-shots/bin/playwright install chromium
    .venv-shots/bin/python scripts/screenshots.py

Nothing in here comes from a real installation: names, times, messages and
the pictures (drawn by the browser) are invented.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "r2_alarm_archive/rootfs/usr/lib"), str(ROOT / "tests")]

from aiohttp import web  # noqa: E402
from conftest import BERLIN, FakeCloudflare, FakeHA, FakeR2, make_config  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from r2_archive import archive as a  # noqa: E402
from r2_archive.app import Archive  # noqa: E402
from r2_archive.cloudflare import Usage  # noqa: E402
from r2_archive.store import Queue, State  # noqa: E402
from r2_archive.web import Cache, Web  # noqa: E402

OUT = ROOT / "docs/screenshots"
LOG = "input_boolean.alarm_log"
PANEL = "alarm_control_panel.home"
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
CAMERAS = {"camera.front_door": ("Front door", "#7fa7d9"), "camera.garden": ("Garden", "#7fa37a")}
VERSION = (ROOT / "r2_alarm_archive/config.yaml").read_text().split('version: "', 1)[1].split('"', 1)[0]

PICTURE = """<!doctype html><body style="margin:0"><div style="width:640px;height:360px;position:relative;
font:20px sans-serif;color:#fff;background:linear-gradient(135deg,{color},#e8eef0)">
<svg viewBox="0 0 24 24" style="position:absolute;left:250px;top:110px;width:140px;fill:rgba(255,255,255,.6)">
<path d="M4,4H7L9,2H15L17,4H20A2,2 0 0,1 22,6V18A2,2 0 0,1 20,20H4A2,2 0 0,1 2,18V6A2,2 0 0,1 4,4M12,7A5,5 0 0,0
7,12A5,5 0 0,0 12,17A5,5 0 0,0 17,12A5,5 0 0,0 12,7M12,9A3,3 0 0,1 15,12A3,3 0 0,1 12,15A3,3 0 0,1 9,12A3,3 0 0,1
12,9Z"/></svg><span style="position:absolute;left:16px;bottom:12px">{name} · demo picture</span></div></body>"""

# (local time, message) per day; a panel change in brackets makes a snapshot
DAYS = {
    "2026-10-28": [("07:42:10", "Disarmed by Sam", ("armed_away", "disarmed")),
                   ("18:05:44", "Armed away by Sam", ("disarmed", "armed_away"))],
    "2026-10-30": [("22:31:02", "Armed night by Alex", ("disarmed", "armed_night")),
                   ("06:58:15", "Disarmed by Alex", None)],
    "2026-11-03": [("08:12:40", "Armed away by Sam", ("disarmed", "armed_away")),
                   ("17:46:03", "Disarmed by Sam", ("armed_away", "disarmed"))],
    "2026-11-06": [("07:15:33", "Disarmed by Sam", ("armed_night", "disarmed")),
                   ("22:40:18", "Armed night by Alex", ("disarmed", "armed_night"))],
    "2026-11-07": [("03:13:21", "ALARM: front door opened while armed night", ("armed_night", "triggered")),
                   ("03:13:58", "Disarmed by Alex after the alarm", ("triggered", "disarmed")),
                   ("15:20:09", "Guest opened the study", ("disarmed", a.REQUEST))],
    "2026-11-12": [("07:30:55", "Disarmed by Sam", ("armed_night", "disarmed")),
                   ("22:48:12", "Arming refused: kitchen window open", None)],
    "2026-11-13": [("08:01:37", "Armed away by Sam", ("disarmed", "armed_away")),
                   ("16:44:20", "Disarmed by Sam", ("armed_away", "disarmed"))],
}


async def pictures(browser) -> dict[str, bytes]:
    page = await browser.new_page(viewport={"width": 640, "height": 360})
    out = {}
    for camera, (name, color) in CAMERAS.items():
        await page.set_content(PICTURE.format(color=color, name=name))
        out[camera] = await page.screenshot(type="jpeg", quality=80)
    await page.close()
    return out


def demo(folder: Path, shots: dict[str, bytes]) -> Archive:
    cfg = make_config(folder, cameras=list(CAMERAS), log_entity=LOG, alarm_panel=PANEL)
    r2 = FakeR2()
    ha = FakeHA()
    ha.state_list = [{"entity_id": "person.sam", "state": "home",
                      "attributes": {"user_id": "u-sam", "friendly_name": "Sam"}},
                     {"entity_id": "person.alex", "state": "home",
                      "attributes": {"user_id": "u-alex", "friendly_name": "Alex"}}]
    archive = Archive(cfg, ha, r2, FakeCloudflare(), Queue(folder / "queue"), State(folder / "state.json"),
                      lambda: NOW)
    archive.tz, archive.language, archive.ha_connected, archive.r2_connected = BERLIN, "en", True, True
    archive.token_checked = True
    archive.guard.update(Usage(1_874, 9_310, 412_000_000, 1_204, 6_877, 96_400_000),
                         asyncio.get_running_loop().time(), (2026, 11))
    archive.state.data["storage_days"] = {
        (NOW - timedelta(days=n)).date().isoformat(): [412_000_000 - n * 2_300_000, 96_400_000 - n * 2_300_000]
        for n in range(8)}
    for day, entries in DAYS.items():
        for clock, message, change in entries:
            moment = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=BERLIN).astimezone(UTC)
            entry = a.log_entry({"entity_id": LOG, "message": message, "when": moment.timestamp()}, LOG)
            key, local_day, body = a.log_object(entry, BERLIN)
            r2.objects[key] = body
            archive.state.uploaded(local_day, key)
            if change:
                snapshot(archive, r2, moment, *change, shots, reason=message if change[1] == a.REQUEST else None)
        if day:
            text = f"Alarm-Protokoll {day}\n".encode()
            r2.objects[a.summary_key(day, 0)] = text
            archive.state.set_summary(day, sorted(archive.state.data["uploaded"].get(day, [])), 1)
    archive.state.data["last_upload"] = (NOW - timedelta(hours=16, minutes=46)).isoformat()
    archive.state.data["last_summary"] = (NOW - timedelta(hours=8, minutes=25)).isoformat()
    # One entry still on its way to R2
    archive.queue.add("protokoll/2026/11/14/20261114T082944.512000Z_1f2e3d4c.json", "log", "2026-11-14",
                      b"{}", "application/json")
    return archive


def snapshot(archive, r2, moment, old, new, shots, reason=None):
    folder, day = a.snapshot_folder(moment, new, BERLIN)
    files = {}
    series = {}
    if new in ("triggered", a.REQUEST):
        for camera in CAMERAS:
            names = [a.camera_file(camera, n) for n in (1, 2, 3)]
            series[camera] = names
            files.update(dict.fromkeys(names, shots[camera]))
    states = [{"entity_id": PANEL, "state": new if new != a.REQUEST else old,
               "attributes": {"friendly_name": "Alarm panel"}, "last_changed": moment.isoformat()},
              {"entity_id": "binary_sensor.front_door", "state": "on" if new == "triggered" else "off",
               "attributes": {"friendly_name": "Front door"}, "last_changed": moment.isoformat()},
              {"entity_id": "binary_sensor.study_door", "state": "on" if new == a.REQUEST else "off",
               "attributes": {"friendly_name": "Study door"},
               "last_changed": (moment - timedelta(hours=3)).isoformat()},
              {"entity_id": "binary_sensor.kitchen_window", "state": "off",
               "attributes": {"friendly_name": "Kitchen window"},
               "last_changed": (moment - timedelta(hours=9)).isoformat()}]
    user = "u-alex" if "Alex" in (reason or "") or moment.hour in range(20, 24) else "u-sam"
    files["meta.json"] = a.meta_body(moment, PANEL, old, new, {}, None if new == "triggered" else user,
                                     {c: s[0] for c, s in series.items()}, False, series, reason)
    files["zustand.json.gz"] = gzip.compress(json.dumps({"states": states}).encode())
    for name, body in files.items():
        r2.objects[folder + name] = body
        archive.state.uploaded(day, folder + name)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as play:
        browser = await play.chromium.launch()
        with tempfile.TemporaryDirectory() as tmp:
            archive = demo(Path(tmp), await pictures(browser))
            page_app = Web(archive, Cache(Path(tmp) / "cache"), allowed_peers=set(), slug="r2_alarm_archive",
                           version=VERSION).app()
            runner = web.AppRunner(page_app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            alarm = next(k for k in archive.state.data["uploaded"]["2026-11-07"] if "_triggered/" in k)
            shots = [
                ("menu", "/", 1200, "light"),
                ("overview", "/overview", 1200, "light"),
                ("details", "/details", 1200, "light"),
                ("alarm-filter", "/details?alarms=1", 1200, "light"),
                ("day", "/day/2026-11-07", 1200, "light"),
                ("snapshot", "/snapshot/2026-11-07/" + alarm.split("/")[4], 1200, "light"),
                ("queue", "/queue", 1200, "light"),
                ("overview-dark", "/overview", 1200, "dark"),
                ("day-phone", "/day/2026-11-07", 390, "light"),
            ]
            for name, path, width, scheme in shots:
                context = await browser.new_context(viewport={"width": width, "height": 600},
                                                    color_scheme=scheme, device_scale_factor=1)
                page = await context.new_page()
                await page.goto(base + path)
                await page.screenshot(path=OUT / f"{name}.png", full_page=True)
                await context.close()
                print(f"{name}.png")
            await runner.cleanup()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
