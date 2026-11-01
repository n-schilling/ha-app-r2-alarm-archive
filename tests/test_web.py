"""The ingress page against the archive with fakes: pages, downloads, cache,
the guard, and that only the ingress proxy gets in."""

import asyncio
import gzip
import json
from datetime import UTC, datetime

import pytest

from conftest import BERLIN, Clock, FakeCloudflare, FakeHA, FakeR2, make_config
from r2_archive import archive as a
from r2_archive.app import Archive
from r2_archive.cloudflare import Usage
from r2_archive.store import Queue, State
from r2_archive.web import Cache, Web, problem_text

LOG = "input_boolean.alarm_log"
DAY = "2026-10-31"
FOLDER = "snapshots/2026/10/31/20261031T101733.000000Z_triggered"


USAGE = Usage(1234, 56, 7_000_000, 1000, 50, 2_000_000)


def seeded(tmp_path, usage=USAGE):
    cfg = make_config(tmp_path)
    r2 = FakeR2()
    archive = Archive(cfg, FakeHA(), r2, FakeCloudflare(), Queue(tmp_path / "queue"),
                      State(tmp_path / "state.json"), Clock(datetime(2026, 11, 1, 9, 0, tzinfo=UTC)))
    archive.tz, archive.language, archive.ha_connected, archive.r2_connected = BERLIN, "de", True, True
    archive.rule_problems = []
    archive.guard.update(usage, asyncio.get_running_loop().time(), (2026, 11))
    for when, message in ((datetime(2026, 10, 31, 10, 17, 44, tzinfo=UTC), "Manuell entschärft"),
                          (datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC), "ALARM <ausgelöst>")):
        entry = a.log_entry({"entity_id": LOG, "message": message, "when": when.timestamp()}, LOG)
        key, day, body = a.log_object(entry, BERLIN)
        r2.objects[key] = body
        archive.state.uploaded(day, key)
    meta = {"time": "2026-10-31T10:17:33+00:00", "from": "armed_night", "to": "triggered", "user_id": None,
            "cameras": {"camera.front": "kamera_front.jpg"}, "missing_cameras": ["camera.back"],
            "reconstructed": False}
    states = {"states": [{"entity_id": "binary_sensor.window_a", "state": "on",
                          "attributes": {"friendly_name": "Window A"}, "last_changed": "2026-10-31T10:17:30+00:00"}]}
    for name, body in (("meta.json", json.dumps(meta).encode()), ("zustand.json.gz", gzip.compress(json.dumps(states).encode())),
                       ("kamera_front.jpg", b"\xff\xd8jpeg")):
        r2.objects[f"{FOLDER}/{name}"] = body
        archive.state.uploaded(DAY, f"{FOLDER}/{name}")
    r2.objects[a.summary_key(DAY, 0)] = b"Alarm-Protokoll 31.10.2026\n"
    archive.state.data["summaries"][DAY] = {"covered": list(archive.state.data["uploaded"][DAY]), "files": 1}
    return archive, r2


@pytest.fixture
async def client(tmp_path, aiohttp_client):
    archive, r2 = seeded(tmp_path)
    web = Web(archive, Cache(tmp_path / "cache"), allowed_peers=set())
    return await aiohttp_client(web.app()), archive, r2


async def text(client, path, headers=None):
    resp = await client.get(path, headers=headers or {})
    assert resp.status == 200, await resp.text()
    return await resp.text()


async def test_overview(client):
    c, archive, _ = client
    page = await text(c, "/", {"X-Ingress-Path": "/api/hassio_ingress/abc"})
    assert "Archivierung läuft" in page and 'href="/api/hassio_ingress/abc/days"' in page
    assert "1.234" in page and "900.000" in page and "7.0 MB" in page and "2.0 MB" in page
    assert "Lifecycle- und Lock-Regeln passen" in page
    archive.language = "en"
    archive.r2_connected = False
    assert "R2 not reachable" in await text(c, "/")


async def test_days_and_day(client):
    c, _, _ = client
    days = await text(c, "/days")
    assert "31.10.2026" in days and "<td class='n'>2</td><td class='n'>1</td><td class='n'>1</td>" in days
    day = await text(c, f"/day/{DAY}")
    # Sorted by time, escaped, in local time
    assert day.index("11:17:33") < day.index("11:17:44")
    assert "ALARM &lt;ausgelöst&gt;" in day and "<ausgelöst>" not in day
    assert f"/snapshot/{DAY}/20261031T101733.000000Z_triggered" in day
    assert "2026-10-31.txt?download=1" in day
    assert (await c.get("/day/kaputt")).status == 404


async def test_snapshot(client):
    c, _, _ = client
    page = await text(c, f"/snapshot/{DAY}/20261031T101733.000000Z_triggered")
    assert "armed_night → triggered" in page and "Window A" in page and "binary_sensor.window_a" in page
    assert f'src="/file/{FOLDER}/kamera_front.jpg"' in page and "camera.back" in page
    assert (await c.get(f"/snapshot/{DAY}/gibt_es_nicht")).status == 404


async def test_files(client):
    c, _, r2 = client
    resp = await c.get(f"/file/{FOLDER}/kamera_front.jpg?download=1")
    assert resp.status == 200 and resp.content_type == "image/jpeg"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="kamera_front.jpg"'
    assert await resp.read() == b"\xff\xd8jpeg"
    summary = await c.get(f"/file/{a.summary_key(DAY, 0)}")
    assert summary.content_type == "text/plain" and "Alarm-Protokoll" in await summary.text()
    for bad in ("/file/other/x.json", "/file/protokoll/../secret"):
        assert (await c.get(bad)).status == 404


async def test_cache_and_guard(client):
    c, archive, r2 = client
    await text(c, f"/day/{DAY}")
    gets = [k for op, k in r2.calls if op == "GET"]
    await text(c, f"/day/{DAY}")
    assert [k for op, k in r2.calls if op == "GET"] == gets          # second time from the cache
    assert archive.guard.own["class_b"] == len(gets)
    # At the limit: cached objects still show, others are marked
    archive.guard.update(Usage(0, 9_000_000, 0), asyncio.get_running_loop().time(), (2026, 11))
    snapshot = await c.get(f"/snapshot/{DAY}/20261031T101733.000000Z_triggered")
    assert "Free-Tier-Grenze ist erreicht" in await snapshot.text()
    assert (await c.get(f"/file/{FOLDER}/kamera_front.jpg")).status == 503
    assert "Manuell entschärft" in await text(c, f"/day/{DAY}")


async def test_queue_page(client):
    c, archive, _ = client
    assert "Nichts wartet" in await text(c, "/queue")
    archive._enqueue("protokoll/2026/11/01/x.json", "log", "2026-11-01", b"x" * 2000, "application/json")
    assert "protokoll/2026/11/01/x.json" in await text(c, "/queue")


async def test_only_ingress(tmp_path, aiohttp_client):
    archive, _ = seeded(tmp_path)
    c = await aiohttp_client(Web(archive, Cache(tmp_path / "cache")).app())
    assert (await c.get("/")).status == 403


async def test_storage_growth(tmp_path):
    archive, _ = seeded(tmp_path)
    web = Web(archive, Cache(tmp_path / "cache"))
    assert web.storage_growth() is None
    archive.state.data["storage_days"] = {"2026-10-25": [1_000_000, 0], "2026-10-31": [7_000_000, 0]}
    assert web.storage_growth() == 1_000_000


def test_cache_limit(tmp_path):
    cache = Cache(tmp_path / "c", max_bytes=10)
    cache.put("protokoll/a", b"123456")
    cache.put("protokoll/b", b"123456")
    assert cache.get("protokoll/a") is None and cache.get("protokoll/b") == b"123456"


def test_problem_texts():
    assert problem_text("3 objects waiting, the oldest for 20 min", "de") == "3 Objekte warten, das älteste seit 20 min"
    assert problem_text("R2 not reachable", "en") == "R2 not reachable"
