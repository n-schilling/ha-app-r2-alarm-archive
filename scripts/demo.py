"""Made-up data for the web page, served by the app's own code inside its
image: scripts/screenshots.py starts the built image with this script (and
tests/fakes.py) mounted, in place of the app's start, and checks and draws
the page it serves. The fakes stand in for Home Assistant, R2 and Cloudflare;
the page itself is the image's, on the ingress port, open only to the
Supervisor's address, with the slug from the host name and the version the
image was built with.

    python3 demo.py <folder with front_door.jpg and garden.jpg>

Nothing in here comes from a real installation: names, times, messages and
the pictures (drawn by scripts/screenshots.py) are invented.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The image's code comes first (PYTHONPATH=/usr/lib); the repository's only outside the image
sys.path += [str(ROOT / "tests"), str(ROOT / "r2_alarm_archive/rootfs/usr/lib")]

from fakes import BERLIN, FakeCloudflare, FakeHA, FakeR2, make_config  # noqa: E402

from r2_archive import archive as a  # noqa: E402
from r2_archive.app import Archive  # noqa: E402
from r2_archive.cloudflare import Usage  # noqa: E402
from r2_archive.store import Queue, State  # noqa: E402
from r2_archive.web import Cache, Web, host_slug  # noqa: E402

LOG = "input_boolean.alarm_log"
PANEL = "alarm_control_panel.home"
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
CAMERAS = ["camera.front_door", "camera.garden"]

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




async def serve(pictures: Path) -> None:
    shots = {camera: (pictures / f"{camera.split('.', 1)[1]}.jpg").read_bytes() for camera in CAMERAS}
    with tempfile.TemporaryDirectory() as tmp:
        archive = demo(Path(tmp), shots)
        await Web(archive, Cache(Path(tmp) / "cache"), slug=host_slug(),
                  version=os.environ.get("APP_VERSION", "")).run()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1])))
