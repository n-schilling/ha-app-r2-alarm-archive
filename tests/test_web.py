"""The ingress page against the archive with fakes: pages, downloads, cache,
the guard, and that only the ingress proxy gets in."""

import asyncio
import gzip
import json
from datetime import UTC, datetime
from urllib.parse import quote

import pytest

from conftest import BERLIN, Clock, FakeCloudflare, FakeHA, FakeR2, make_config
from r2_archive import archive as a
from r2_archive.app import Archive, Problem
from r2_archive.cloudflare import Usage
from r2_archive.guard import LIMITS
from r2_archive.store import Queue, State
from r2_archive.web import SCRIPT, Cache, Web, lang_of, problem_text

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
    archive.ha.state_list = [{"entity_id": "person.kim", "state": "home",
                              "attributes": {"user_id": "u1", "friendly_name": "Kim"}}]
    archive.guard.update(usage, asyncio.get_running_loop().time(), (2026, 11))
    for when, message in ((datetime(2026, 10, 31, 10, 17, 44, tzinfo=UTC), "Manuell entschärft"),
                          (datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC), "ALARM <ausgelöst>")):
        entry = a.log_entry({"entity_id": LOG, "message": message, "when": when.timestamp()}, LOG)
        key, day, body = a.log_object(entry, BERLIN)
        r2.objects[key] = body
        archive.state.uploaded(day, key)
    meta = {"time": "2026-10-31T10:17:33+00:00", "from": "armed_night", "to": "triggered", "user_id": "u1",
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
    web = Web(archive, Cache(tmp_path / "cache"), allowed_peers=set(), slug="local_r2_alarm_archive",
              version="9.8.7")
    return await aiohttp_client(web.app()), archive, r2


async def text(client, path, headers=None):
    resp = await client.get(path, headers=headers or {})
    assert resp.status == 200, await resp.text()
    return await resp.text()


async def test_menu(client):
    c, archive, _ = client
    page = await text(c, "/", {"X-Ingress-Path": "/api/hassio_ingress/abc"})
    for name in ("overview", "details", "queue"):
        assert f'href="/api/hassio_ingress/abc/{name}"' in page
    # The options are Home Assistant's own page, outside the ingress frame
    assert 'href="/config/app/local_r2_alarm_archive/config" target="_top"' in page
    assert "Detailansicht" in page and "<span class=\"badge" not in page
    archive.r2_connected = False
    archive._enqueue("protokoll/2026/11/01/x.json", "log", "2026-11-01", b"x", "application/json")
    page = await text(c, "/")
    assert '<span class="badge warn">1</span>' in page and '<span class="badge">1</span>' in page


async def test_overview(client):
    c, archive, _ = client
    page = await text(c, "/overview", {"X-Ingress-Path": "/api/hassio_ingress/abc"})
    assert "Archivierung läuft" in page and 'class="icon-button back" href="/api/hassio_ingress/abc/"' in page
    assert "1.234" in page and "900.000" in page and "7,0 MB" in page and "2,0 MB" in page and "0,12 %" in page
    archive.language = "en"
    archive.r2_connected = False
    assert "R2 not reachable" in await text(c, "/overview")


async def test_details_by_month(client):
    c, archive, _ = client
    archive.state.uploaded("2026-09-30", "protokoll/2026/09/30/x.json")
    page = await text(c, "/details")
    # Newest month open, older ones closed; the day counts entries, snapshots, alarms, summaries
    assert '<details id="m-2026-10" open>' in page and '<details id="m-2026-09">' in page
    assert "Oktober 2026" in page and "September 2026" in page and "Sa. 31.10.2026" in page
    assert "<div class='n'>2</div><div class='n'>1</div><div class='n alarm'>1</div><div class='n opt'>1</div>" in page
    assert '<details id="m-2026-09" open>' in await text(c, "/details?month=2026-09")
    assert "Noch nichts archiviert" not in page


async def test_day(client):
    c, _, _ = client
    day = await text(c, f"/day/{DAY}")
    assert 'href="/details?month=2026-10"' in day
    # Sorted by time, escaped, in local time
    assert day.index("11:17:33") < day.index("11:17:44")
    assert "ALARM &lt;ausgelöst&gt;" in day and "<ausgelöst>" not in day
    assert f"/snapshot/{DAY}/20261031T101733.000000Z_triggered" in day and "'alarm'>ALARM</div>" in day
    assert "2026-10-31.txt?download=1" in day
    assert (await c.get("/day/kaputt")).status == 404


async def test_snapshot(client):
    c, _, _ = client
    page = await text(c, f"/snapshot/{DAY}/20261031T101733.000000Z_triggered")
    assert "Nacht scharf → ALARM" in page and "Window A" in page and "binary_sensor.window_a" in page
    assert "<td>Kim</td>" in page
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
    waiting = Problem("waiting", count=3, minutes=20)
    assert waiting == "3 objects waiting, the oldest for 20 min"
    assert problem_text(waiting, "de") == "3 Objekte warten, das älteste seit 20 min"
    assert problem_text(Problem("summary", day="2026-10-31"), "de") == "Tageszusammenfassung vom 31.10.2026 fehlt"
    assert problem_text(Problem("r2_check", error="HEAD: HTTP 403"), "de") == \
        "Prüfung von R2 fehlgeschlagen: HEAD: HTTP 403"
    assert problem_text(Problem("r2"), "en") == "R2 not reachable"
    # Every problem has a German text
    from r2_archive.app import PROBLEMS
    from r2_archive.web import PROBLEMS_DE
    assert set(PROBLEMS) == set(PROBLEMS_DE)


async def test_user_without_person(tmp_path):
    archive, _ = seeded(tmp_path)
    archive.ha.users = [{"id": "wd", "name": "Wall Display"}]
    web = Web(archive, Cache(tmp_path / "cache"))
    assert await web.user_name("wd") == "Wall Display"
    assert await web.user_name("u1") == "Kim"
    assert await web.user_name("nobody") == "nobody"


async def test_alarm_filter(client):
    c, archive, _ = client
    archive.state.uploaded("2026-10-30", "protokoll/2026/10/30/x.json")
    archive.state.uploaded("2026-09-30", "protokoll/2026/09/30/x.json")
    page = await text(c, "/details?alarms=1")
    assert "Sa. 31.10.2026" in page and "Fr. 30.10.2026" not in page and "September 2026" not in page
    assert 'class="chip on" href="/details"' in page
    archive.state.data["uploaded"]["2026-10-31"] = [k for k in archive.state.data["uploaded"]["2026-10-31"]
                                                    if "triggered" not in k]
    assert "Kein Tag mit Alarm." in await text(c, "/details?alarms=1")


async def test_export(client):
    import io
    import zipfile
    c, archive, r2 = client
    day = await text(c, f"/day/{DAY}")
    assert f'href="/export/{DAY}.zip"' in day
    assert 'href="/export/2026-10.zip"' in await text(c, "/details")
    # A snapshot file R2 no longer has, e.g. after the retention
    archive.state.uploaded(DAY, f"{FOLDER}/kamera_back.jpg")
    resp = await c.get(f"/export/{DAY}.zip")
    assert resp.status == 200 and resp.headers["Content-Disposition"] == f'attachment; filename="alarm-archiv-{DAY}.zip"'
    names = zipfile.ZipFile(io.BytesIO(await resp.read())).namelist()
    root = f"alarm-archiv-{DAY}/"
    assert root + f"{FOLDER}/kamera_front.jpg" in names and root + a.summary_key(DAY, 0) in names
    assert sum(n.startswith(root + "protokoll/") for n in names) == 2
    zipped = zipfile.ZipFile(io.BytesIO(await (await c.get(f"/export/{DAY}.zip")).read()))
    assert "ALARM <ausgelöst>" in zipped.read(root + f"protokoll-{DAY}.txt").decode()
    assert zipped.read(root + "fehlende-dateien.txt").decode() == f"{FOLDER}/kamera_back.jpg\n"
    month = zipfile.ZipFile(io.BytesIO(await (await c.get("/export/2026-10.zip")).read())).namelist()
    assert "alarm-archiv-2026-10/" + f"{FOLDER}/meta.json" in month
    for bad in ("/export/2026-13.zip", "/export/1999-01.zip", "/export/kaputt.zip"):
        assert (await c.get(bad)).status == 404


async def test_quarantine_on_queue_page(client):
    c, archive, _ = client
    archive.queue.quarantine.mkdir(parents=True)
    (archive.queue.quarantine / "0000000007.json").write_text("kaputt")
    page = await text(c, "/queue")
    assert "Quarantäne" in page and "0000000007.json" in page
    assert "/addon_configs/local_r2_alarm_archive/quarantine" in page
    assert "1 Objekte in Quarantäne" in await text(c, "/overview")


async def test_token_on_overview(client):
    from datetime import timedelta
    c, archive, _ = client
    assert "noch nicht geprüft" in await text(c, "/overview")
    archive.token_checked, archive.token_expiry = True, None
    assert "<td>nie</td>" in await text(c, "/overview")
    archive.token_expiry = archive.now() + timedelta(days=5)
    assert "class=\"bad\">06.11.2026 10:00:00 (in 5 Tagen)" in await text(c, "/overview")


async def test_series_pictures(client):
    c, archive, r2 = client
    meta = json.loads(r2.objects[f"{FOLDER}/meta.json"])
    meta["series"] = {"camera.front": ["kamera_front.jpg", "kamera_front_2.jpg", "kamera_front_3.jpg"]}
    r2.objects[f"{FOLDER}/meta.json"] = json.dumps(meta).encode()
    r2.objects[f"{FOLDER}/kamera_front_2.jpg"] = b"\xff\xd8two"
    archive.state.uploaded(DAY, f"{FOLDER}/kamera_front_2.jpg")
    page = await text(c, f"/snapshot/{DAY}/20261031T101733.000000Z_triggered")
    # Picture 3 never arrived: not shown
    assert "kamera_front_2.jpg" in page and "kamera_front_3.jpg" not in page and "camera.front · 2" in page


REQUEST = "20261031T121500.000000Z_request"


def add_request(archive, r2, reason="Guest opened the study"):
    folder = f"snapshots/2026/10/31/{REQUEST}"
    meta = {"time": "2026-10-31T12:15:00+00:00", "panel": "alarm_control_panel.home", "from": "disarmed",
            "to": "request", "reason": reason, "user_id": None, "cameras": {"camera.front": "kamera_front.jpg"},
            "missing_cameras": [], "reconstructed": False}
    for name, body in (("meta.json", json.dumps(meta).encode()),
                       ("zustand.json.gz", gzip.compress(json.dumps({"states": []}).encode())),
                       ("kamera_front.jpg", b"\xff\xd8req")):
        r2.objects[f"{folder}/{name}"] = body
        archive.state.uploaded(DAY, f"{folder}/{name}")


async def test_snapshot_on_request(client):
    c, archive, r2 = client
    add_request(archive, r2, "Guest <opened> the study")
    day = await text(c, f"/day/{DAY}")
    # Labelled, with the reason, and not marked as an alarm
    assert ("<div class=''>Auf Anfrage <span class=\"muted\">· Guest &lt;opened&gt; the study</span></div>"
            in day)
    page = await text(c, f"/snapshot/{DAY}/{REQUEST}")
    assert "Unscharf → Auf Anfrage" in page and "<th>Grund</th><td>Guest &lt;opened&gt; the study</td>" in page
    assert f'src="/file/snapshots/2026/10/31/{REQUEST}/kamera_front.jpg"' in page
    archive.language = "en"
    assert "on request" in await text(c, f"/snapshot/{DAY}/{REQUEST}")
    assert "<th>Reason</th>" in await text(c, f"/snapshot/{DAY}/{REQUEST}")


async def test_request_is_no_alarm(client, tmp_path):
    c, archive, r2 = client
    add_request(archive, r2)
    archive.state.data["uploaded"][DAY] = [k for k in archive.state.data["uploaded"][DAY] if "triggered" not in k]
    web = Web(archive, Cache(tmp_path / "cache2"))
    assert web.day_counts(DAY)[1:3] == (1, 0)
    assert "<div class='n'>1</div><div class='n'>–</div>" in await text(c, "/details")
    assert "Kein Tag mit Alarm." in await text(c, "/details?alarms=1")


async def test_request_without_panel_or_reason(client):
    c, archive, r2 = client
    add_request(archive, r2, reason=None)
    meta = json.loads(r2.objects[f"snapshots/2026/10/31/{REQUEST}/meta.json"])
    meta["from"] = None
    r2.objects[f"snapshots/2026/10/31/{REQUEST}/meta.json"] = json.dumps(meta).encode()
    page = await text(c, f"/snapshot/{DAY}/{REQUEST}")
    assert "– → Auf Anfrage" in page and "Grund" not in page
    assert "<div class=''>Auf Anfrage</div>" in await text(c, f"/day/{DAY}")


# --- Review fixes -----------------------------------------------------------------
async def test_version_at_the_bottom_of_every_page(client):
    c, _, _ = client
    for path in ("/", "/overview", "/details", f"/day/{DAY}", f"/snapshot/{DAY}/{FOLDER.rsplit('/', 1)[1]}",
                 "/queue"):
        page = await text(c, path)
        assert page.endswith('</main><footer class="version">R2 Alarm Archive 9.8.7</footer></body></html>'), path


PAGES = ("/", "/overview", "/details", f"/day/{DAY}", f"/snapshot/{DAY}/{FOLDER.rsplit('/', 1)[1]}", "/queue")


async def test_same_header_on_every_page(client, tmp_path):
    """STANDARD.md §7: name (with a back arrow on a subpage), then stamp,
    refresh and the app's options, and nothing else in the header."""
    import re
    c, archive, _ = client
    gear = ('<a class="icon-button" href="/config/app/local_r2_alarm_archive/config" target="_top" '
            'title="Einstellungen">')
    for path in PAGES:
        page = await text(c, path, {"X-Ingress-Path": "/api/hassio_ingress/abc"})
        header = page.split("<header>", 1)[1].split("</header>", 1)[0]
        start, actions = header.split('<div class="actions">', 1)
        if path == "/":
            assert start == "<h1>R2 Alarm Archive</h1>"
        else:
            assert re.fullmatch(r'<a class="icon-button back" href="/api/hassio_ingress/abc/[^"]*" '
                                r'title="Zurück"><svg[^>]*><path d="[^"]+"/></svg></a><h1>[^<]+</h1>', start), path
        assert re.fullmatch(r'<span class="stamp opt">Stand \d\d:\d\d:\d\d</span>'
                            r'<a class="icon-button" id="refresh" href="" role="button" title="Aktualisieren">'
                            r'<svg[^>]*><path d="[^"]+"/></svg></a>' + re.escape(gear) +
                            r'<svg[^>]*><path d="[^"]+"/></svg></a></div>', actions), path
    archive.language = "en"
    page = await text(c, "/")
    assert "As of " in page and 'title="Refresh"' in page and 'title="Settings">' in page
    # The stamp is the time of the request in the app's time zone
    assert f"As of {archive.now().astimezone(archive.tz).strftime('%H:%M:%S')}<" in page


async def test_no_settings_without_slug(tmp_path, aiohttp_client):
    archive, _ = seeded(tmp_path)
    c = await aiohttp_client(Web(archive, Cache(tmp_path / "cache"), allowed_peers=set()).app())
    page = await text(c, "/overview")
    assert 'id="refresh"' in page and "/config/app/" not in page


async def test_day_export_in_the_body(client):
    c, _, _ = client
    page = await text(c, f"/day/{DAY}", {"X-Ingress-Path": "/api/hassio_ingress/abc"})
    header, body = page.split("</header>", 1)
    assert "export" not in header
    assert f'<div class="card-actions"><a class="button" href="/api/hassio_ingress/abc/export/{DAY}.zip">' in body


async def test_refresh_reloads_in_the_script():
    from r2_archive.web import SCRIPT
    assert 'e.target.closest("#refresh")' in SCRIPT and "location.reload()" in SCRIPT


async def test_security_headers(client):
    import base64
    import hashlib

    from r2_archive.web import SCRIPT
    c, _, _ = client
    for path in ("/", f"/file/{FOLDER}/kamera_front.jpg"):
        resp = await c.get(path)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        csp = resp.headers["Content-Security-Policy"]
        digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        assert f"script-src 'sha256-{digest}'" in csp and "default-src 'none'" in csp
        assert "frame-ancestors 'self'" in csp
    # The page carries exactly that script
    assert f"<script>{SCRIPT}</script>" in await text(c, "/")


async def test_foreign_keys_never_reach_the_page(client):
    """A key with HTML in its folder (from a bucket somebody else wrote to)
    is neither linked nor served, and an export skips a path that climbs out."""
    import io
    import zipfile
    c, archive, r2 = client
    evil = 'snapshots/2026/10/31/20261031T000000.000000Z_x"><svg onload=alert(1)>'
    slip = "snapshots/2026/10/31/../../../../../x.plist"
    for key in (f"{evil}/meta.json", slip):
        r2.objects[key] = b"{}"
        archive.state.uploaded(DAY, key)
    page = await text(c, f"/day/{DAY}")
    assert "onload" not in page and "20261031T000000" not in page
    assert (await c.get(f"/snapshot/{DAY}/" + quote(evil.rsplit("/", 1)[1], safe=""))).status == 404
    assert (await c.get(f"/file/{evil}/meta.json")).status == 404
    assert (await c.get("/file/snapshots/2026/10/31/x/meta.json")).status == 404
    names = zipfile.ZipFile(io.BytesIO(await (await c.get(f"/export/{DAY}.zip")).read())).namelist()
    assert not any(".." in n or "onload" in n for n in names)


async def test_snapshot_links_are_encoded(client):
    c, _, _ = client
    page = await text(c, f"/snapshot/{DAY}/{FOLDER.rsplit('/', 1)[1]}", {"X-Ingress-Path": '/a"b'})
    assert '/a&quot;b/file/' in page and '/a"b' not in page


async def test_no_bucket_rules_on_the_page(client):
    """The bucket's rules are the Cloudflare account's business: the page says nothing about them."""
    c, _, _ = client
    page = await text(c, "/overview")
    assert "Bucket-Regeln" not in page and "Bucket-Sperre" not in page and "wrangler" not in page


async def test_export_reads_past_the_cache_and_needs_the_guard(tmp_path, aiohttp_client):
    archive, _ = seeded(tmp_path)
    c = await aiohttp_client(Web(archive, Cache(tmp_path / "cache"), allowed_peers=set()).app())
    assert (await c.get(f"/export/{DAY}.zip")).status == 200
    # Nothing of the export went into the page's cache, and no temporary file is left
    assert not list((tmp_path / "cache").iterdir())
    assert not list(tmp_path.glob(".export-*"))
    # Not enough reads left for the whole export: none at all
    archive.guard.update(Usage(0, LIMITS["class_b"] - 1, 0), asyncio.get_running_loop().time(), (2026, 11))
    assert (await c.get(f"/export/{DAY}.zip")).status == 503


def test_cache_counts_its_size(tmp_path):
    cache = Cache(tmp_path / "c", max_bytes=10)
    cache.put("a", b"12345")
    cache.put("a", b"123")             # replaced, not added
    cache.put("b", b"1234567")
    assert cache.get("a") == b"123" and cache.get("b") == b"1234567"
    cache.put("c", b"12")
    assert sum(p.stat().st_size for p in (tmp_path / "c").iterdir()) <= 10
    assert Cache(tmp_path / "c", max_bytes=10)._total == cache._total


async def test_shared_stylesheet_first(client):
    """ha.css, the same file in every app, comes unchanged and first; the
    page's own rules follow in a second block."""
    from pathlib import Path

    c, _, _ = client
    shared = (Path(__file__).parents[1] / "r2_alarm_archive/rootfs/usr/local/share/ha-app/ha.css").read_text()
    page = await text(c, "/")
    assert f"<style>{shared}</style><style>" in page
    assert "footer.version" in shared


def test_language_of_home_assistant():
    # The cookie of share/ha-app/ha-lang.js decides; then Home Assistant's
    # configured language; the browser's languages only while that is unknown
    assert lang_of("a=b; ha_lang=de", "en", "en-US") == "de"
    assert lang_of("ha_lang=en", "de", "de-DE,de") == "en"
    assert lang_of("ha_lang=fr", "de", "de-DE,de") == "en"
    assert lang_of(None, "de-CH", "en") == "de"
    assert lang_of("other=1", "en", "de") == "en"
    assert lang_of(None, "", "de-DE,de;q=0.9,en;q=0.8") == "de"
    assert lang_of(None, "", "fr,en;q=0.5,de;q=0.4") == "en"
    assert lang_of(None, "", None) == "en"


async def test_page_in_the_viewers_language(client):
    c, archive, _ = client
    archive.language = "de"
    assert '<html lang="en">' in await text(c, "/overview", {"Cookie": "ha_lang=en", "Accept-Language": "de"})
    assert '<html lang="en">' in await text(c, "/overview", {"Cookie": "ha_lang=fr"})
    archive.language = "en"
    page = await text(c, "/overview", {"Cookie": "ha_lang=de", "Accept-Language": "en"})
    assert '<html lang="de">' in page and "Archivierung läuft" in page
    # Without the cookie, Home Assistant's configured language
    assert '<html lang="en">' in await text(c, "/overview", {"Accept-Language": "de"})


async def test_shared_language_script_first(client):
    """ha-lang.js, the same file in every app, opens the page's one script."""
    from pathlib import Path

    c, _, _ = client
    shared = (Path(__file__).parents[1] / "r2_alarm_archive/rootfs/usr/local/share/ha-app/ha-lang.js").read_text()
    assert "ha_lang" in shared and SCRIPT.startswith(shared)
    assert f"<script>{SCRIPT}</script>" in await text(c, "/")
