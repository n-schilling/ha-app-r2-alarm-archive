"""The archive with fakes for Home Assistant, R2 and Cloudflare: the log
stream, uploads past the guard, snapshots live and rebuilt, summaries."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import BERLIN, Clock, FakeCloudflare, FakeHA, FakeR2, make_config, unzip
from r2_archive import app as app_module
from r2_archive.app import Archive
from r2_archive.cloudflare import Usage
from r2_archive.r2 import R2Error
from r2_archive.store import Queue, State

LOG = "input_boolean.alarm_log"
PANEL = "alarm_control_panel.home"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(app_module, "BACKOFF", (0, 0, 0, 0))


NO_USAGE = Usage(0, 0, 0)


def build(tmp_path, cfg=None, guard_usage=NO_USAGE, **extra):
    cfg = cfg or make_config(tmp_path, **extra)
    ha, r2, cf, clock = FakeHA(), FakeR2(), FakeCloudflare(), Clock()
    archive = Archive(cfg, ha, r2, cf, Queue(tmp_path / "queue"), State(tmp_path / "state.json"), clock)
    archive.tz = BERLIN
    archive._ready.set()
    archive._restored.set()
    if guard_usage is not None:
        archive.guard.update(guard_usage, asyncio.get_running_loop().time(), (2026, 10))
    return archive, ha, r2, cf, clock


def log_event(*entries):
    return {"events": [{"entity_id": LOG, "name": "Alarm", "message": m, "when": w} for w, m in entries]}


async def settle(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running(*coros):
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        yield tasks
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


T1 = datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC).timestamp()
T2 = T1 + 11


async def test_log_is_queued_and_uploaded(tmp_path):
    archive, ha, r2, _, clock = build(tmp_path)
    async with running(archive.follow_log(), archive.upload_loop()):
        await settle(lambda: ha.log_start is not None)
        assert ha.log_start == (clock() - timedelta(days=400)).isoformat()
        await ha.log_events.put(log_event((T1, "ALARM ausgelöst"), (T2, "Manuell entschärft")))
        await ha.log_events.put({"events": [{"entity_id": LOG, "state": "on", "when": T2 + 1}]})
        await settle(lambda: len(r2.objects) == 2 and not len(archive.queue))
    keys = sorted(r2.objects)
    assert all(k.startswith("protokoll/2026/10/31/") for k in keys)
    assert json.loads(r2.objects[keys[0]])["message"] == "ALARM ausgelöst"
    assert archive.state.data["log_cursor"] == T2
    assert sorted(archive.state.data["uploaded"]["2026-10-31"]) == keys
    assert archive.r2_connected is True and archive.state.data["last_upload"]

    # A restart resumes a second before the newest entry; entries seen again
    # are not queued a second time
    again, ha2, r2b, _, _ = build(tmp_path)
    async with running(again.follow_log()):
        await settle(lambda: ha2.log_start is not None)
        assert ha2.log_start == datetime.fromtimestamp(T2 - 1, UTC).isoformat()
        await ha2.log_events.put(log_event((T2, "Manuell entschärft")))
        await asyncio.sleep(0.05)
    assert len(again.queue) == 0


async def test_existing_object_counts_as_uploaded(tmp_path):
    archive, ha, r2, _, _ = build(tmp_path)
    from r2_archive import archive as a
    entry = a.log_entry(log_event((T1, "ALARM"))["events"][0], LOG)
    key, day, body = a.log_object(entry, BERLIN)
    r2.objects[key] = body
    archive._enqueue(key, "log", day, body, "application/json")
    async with running(archive.upload_loop()):
        await settle(lambda: not len(archive.queue))
    assert archive.state.data["uploaded"][day] == [key]
    assert archive.guard.own["storage"] == 0


async def test_guard_holds_uploads_back(tmp_path):
    archive, _, r2, _, _ = build(tmp_path, guard_usage=None)
    archive._enqueue("protokoll/2026/10/31/x.json", "log", "2026-10-31", b"x", "application/json")
    async with running(archive.upload_loop()):
        await asyncio.sleep(0.05)
        assert r2.objects == {} and len(archive.queue) == 1
        assert archive.status()["free_tier_limit"] is None          # no figure, no known limit
        archive.guard.update(Usage(899_999, 0, 0), asyncio.get_running_loop().time(), (2026, 10))
        archive._wake.set()
        await asyncio.sleep(0.05)
        assert r2.objects == {} and archive.status()["free_tier_limit"] is True
        archive.guard.update(Usage(10, 0, 0), asyncio.get_running_loop().time(), (2026, 10))
        archive._wake.set()
        await settle(lambda: not len(archive.queue))
    assert archive.guard.own["class_a"] == 1 and archive.status()["free_tier_usage"] == 0.0


async def test_r2_unreachable_keeps_the_queue(tmp_path):
    archive, _, r2, _, _ = build(tmp_path)
    r2.fail = R2Error("PUT x: Cannot connect")
    archive._enqueue("protokoll/2026/10/31/x.json", "log", "2026-10-31", b"x", "application/json")
    async with running(archive.upload_loop()):
        await settle(lambda: archive.r2_connected is False)
        assert len(archive.queue) == 1
        # An error answer means R2 is there
        r2.fail = R2Error("PUT x: HTTP 400", 400, "InvalidArgument")
        await settle(lambda: archive.r2_connected is True)
        r2.fail = None
        await settle(lambda: not len(archive.queue))


def panel_event(old, new, when, user="u1"):
    return {"variables": {"trigger": {
        "from_state": {"state": old},
        "to_state": {"state": new, "last_changed": when.isoformat(), "attributes": {"changed_by": None},
                     "context": {"user_id": user}}}}}


async def test_live_snapshot(tmp_path):
    archive, ha, r2, _, clock = build(tmp_path, cameras=["camera.front", "camera.back"])
    ha.state_list = [
        {"entity_id": PANEL, "state": "triggered", "attributes": {}, "last_changed": "t"},
        {"entity_id": "binary_sensor.doors_and_windows", "state": "on",
         "attributes": {"entity_id": ["binary_sensor.window_a", "binary_sensor.window_b"]}},
        {"entity_id": "binary_sensor.window_a", "state": "on", "attributes": {}},
        {"entity_id": "binary_sensor.window_b", "state": "off", "attributes": {}},
        {"entity_id": "light.unrelated", "state": "on", "attributes": {}},
    ]
    when = datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC)
    clock.moment = when - timedelta(minutes=10)
    async with running(archive.follow_panel(), archive.upload_loop()):
        await settle(lambda: archive.state.data["panel_cursor"] is not None)
        await ha.panel_events.put(panel_event("armed_night", "arming", when - timedelta(minutes=5)))
        await ha.panel_events.put(panel_event("armed_night", "triggered", when))
        await settle(lambda: len(r2.objects) == 3)
    folder = "snapshots/2026/10/31/20261031T101733.000000Z_triggered/"
    assert sorted(r2.objects) == [folder + "kamera_front.jpg", folder + "meta.json", folder + "zustand.json.gz"]
    states = unzip(r2.objects[folder + "zustand.json.gz"])
    assert [s["entity_id"] for s in states["states"]] == [
        PANEL, "binary_sensor.doors_and_windows", "binary_sensor.window_a", "binary_sensor.window_b"]
    meta = json.loads(r2.objects[folder + "meta.json"])
    assert meta["from"] == "armed_night" and meta["to"] == "triggered" and meta["user_id"] == "u1"
    assert meta["missing_cameras"] == ["camera.back"] and not meta["reconstructed"]
    assert archive.state.data["panel_cursor"] == when.isoformat()


async def test_missed_snapshot_is_rebuilt(tmp_path):
    archive, ha, r2, _, _ = build(tmp_path)
    cursor = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    archive.state.data["panel_cursor"] = cursor.isoformat()
    armed = datetime(2026, 10, 30, 21, 36, 48, tzinfo=UTC)
    ha.state_list = [{"entity_id": PANEL, "state": "armed_night", "attributes": {}},
                     {"entity_id": "binary_sensor.doors_and_windows", "state": "off", "attributes": {}}]
    ha.history_data = {
        PANEL: [{"s": "disarmed", "lu": cursor.timestamp() - 100},
                {"s": "armed_night", "a": {"previous_state": "disarmed"}, "lu": armed.timestamp()}],
        "binary_sensor.doors_and_windows": [{"s": "off", "a": {}, "lu": cursor.timestamp() - 500}],
    }
    async with running(archive.follow_panel(), archive.upload_loop()):
        await settle(lambda: len(r2.objects) == 2)
    folder = "snapshots/2026/10/30/20261030T213648.000000Z_armed_night/"
    assert sorted(r2.objects) == [folder + "meta.json", folder + "zustand.json.gz"]
    meta = json.loads(r2.objects[folder + "meta.json"])
    assert meta["reconstructed"] and meta["missing_cameras"] == ["camera.front"] and meta["from"] == "disarmed"
    states = unzip(r2.objects[folder + "zustand.json.gz"])
    # The fake answers every history call alike; the states come from its first record each
    assert states["reconstructed"]
    assert {s["entity_id"]: s["state"] for s in states["states"]}["binary_sensor.doors_and_windows"] == "off"
    assert ha.history_calls[1][1] == armed.isoformat()
    assert archive.state.data["panel_cursor"] == armed.isoformat()


async def test_daily_summary_and_supplement(tmp_path):
    archive, ha, r2, _, clock = build(tmp_path)
    archive.state.data["installed"] = "2026-10-30"
    async with running(archive.follow_log(), archive.upload_loop()):
        await ha.log_events.put(log_event((T1, "ALARM ausgelöst"), (T2, "Manuell entschärft")))
        await settle(lambda: len(r2.objects) == 2 and not len(archive.queue))

    # Before 00:05 of the next day only the day before gets its summary
    clock.moment = datetime(2026, 10, 31, 23, 2, tzinfo=UTC)      # 00:02 in Berlin
    assert archive._summary_days(datetime(2026, 11, 1).date()) == ["2026-10-30", "2026-10-31"]
    assert await archive.summarize("2026-10-30")
    assert "(keine Einträge)" in r2.objects["zusammenfassung/2026/10/2026-10-30.txt"].decode()

    clock.moment = datetime(2026, 10, 31, 23, 6, tzinfo=UTC)
    assert await archive.summarize("2026-10-31")
    text = r2.objects["zusammenfassung/2026/10/2026-10-31.txt"].decode()
    assert "31.10.2026 11:17:33 - ALARM ausgelöst" in text and "2 Einträge" in text
    assert archive._summary_days(datetime(2026, 11, 1).date()) == []

    # An entry for the 31st that arrives later goes into a supplement
    late = T2 + 3600
    archive._enqueue(*_log_args(late, "Nachgeholt"))
    assert archive._summary_days(datetime(2026, 11, 1).date()) == []          # waits for the queue
    async with running(archive.upload_loop()):
        await settle(lambda: not len(archive.queue))
    assert archive._summary_days(datetime(2026, 11, 1).date()) == ["2026-10-31"]
    assert await archive.summarize("2026-10-31")
    supplement = r2.objects["zusammenfassung/2026/10/2026-10-31-nachtrag-1.txt"].decode()
    assert "Nachgeholt" in supplement and "ALARM" not in supplement and "1 Einträge" in supplement
    assert archive.state.data["summaries"]["2026-10-31"]["files"] == 2


async def test_summary_after_lost_state_does_not_overwrite(tmp_path):
    archive, _, r2, _, _ = build(tmp_path)
    r2.objects["zusammenfassung/2026/10/2026-10-30.txt"] = b"old"
    assert await archive.summarize("2026-10-30")
    assert r2.objects["zusammenfassung/2026/10/2026-10-30.txt"] == b"old"
    assert "zusammenfassung/2026/10/2026-10-30-nachtrag-1.txt" in r2.objects


async def test_summary_waits_for_the_guard(tmp_path):
    archive, _, r2, _, _ = build(tmp_path, guard_usage=None)
    assert not await archive.summarize("2026-10-30")
    assert r2.calls == []


async def test_rules_loop_reports(tmp_path):
    archive, _, _, cf, _ = build(tmp_path)
    async with running(archive.rules_loop()):
        await settle(lambda: archive.rule_problems is not None)
    status = archive.status()
    assert status["bucket_rules"] is True
    attributes = json.loads(status["bucket_rules/attributes"])
    assert attributes["commands"] == [
        "npx wrangler r2 bucket lifecycle add alarm-archive delete-snapshots snapshots/ --expire-days 365"]


def _log_args(when, message):
    from r2_archive import archive as a
    entry = a.log_entry({"entity_id": LOG, "message": message, "when": when}, LOG)
    key, day, body = a.log_object(entry, BERLIN)
    return key, "log", day, body, "application/json"


async def test_health(tmp_path, monkeypatch):
    archive, _, r2, _, clock = build(tmp_path)
    archive.state.data["installed"] = "2026-10-31"
    assert archive.health() == ["Home Assistant not connected"]
    archive.ha_connected = True
    status = archive.status()
    assert status["archiving"] is True and json.loads(status["archiving/attributes"]) == {"problems": []}

    # A failing upload shows at once, and clears with the next success
    r2.fail = R2Error("PUT x: HTTP 403 AccessDenied", 403, "AccessDenied")
    archive._enqueue("protokoll/2026/10/31/x.json", "log", "2026-10-31", b"x", "application/json")
    async with running(archive.upload_loop()):
        await settle(lambda: archive.upload_error is not None)
        assert archive.status()["archiving"] is False
        assert archive.health() == ["last upload failed: PUT x: HTTP 403 AccessDenied"]
        r2.fail = None
        await settle(lambda: not len(archive.queue))
    assert archive.health() == []

    # Something waiting longer than 15 minutes
    archive._enqueue("protokoll/2026/10/31/y.json", "log", "2026-10-31", b"y", "application/json")
    queued = archive.queue.oldest()
    monkeypatch.setattr(app_module.clock_time, "time", lambda: queued + 16 * 60)
    assert archive.health() == ["1 objects waiting, the oldest for 16 min"]
    monkeypatch.setattr(app_module.clock_time, "time", lambda: queued)
    archive.queue.done(archive.queue.items()[0])

    # The guard at its limit, or without a figure
    archive.guard.update(Usage(900_000, 0, 0), asyncio.get_running_loop().time(), (2026, 10))
    assert archive.health() == ["free tier limit reached"]
    archive.guard.usage = None
    assert archive.health() == ["no R2 usage figure for the free tier guard"]
    archive.guard.update(Usage(0, 0, 0), asyncio.get_running_loop().time(), (2026, 10))

    # The summary of the 31st is due at 00:05 and late after 01:05 (Berlin)
    # (a new month needs a new usage figure)
    clock.moment = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    assert archive.health() == ["no R2 usage figure for the free tier guard"]
    archive.guard.update(Usage(0, 0, 0), asyncio.get_running_loop().time(), (2026, 11))
    assert archive.health() == []
    clock.moment = datetime(2026, 11, 1, 0, 10, tzinfo=UTC)
    assert archive.health() == ["summary of 2026-10-31 missing"]
    assert await archive.summarize("2026-10-31")
    assert archive.health() == []


async def test_usage_poll_at_the_month_change(tmp_path):
    archive, _, _, _, clock = build(tmp_path)
    clock.moment = datetime(2026, 10, 31, 23, 55, tzinfo=UTC)
    assert archive._next_usage_poll() == 305
    clock.moment = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert archive._next_usage_poll() == 6
    clock.moment = datetime(2026, 11, 1, 0, 0, 5, tzinfo=UTC)
    assert archive._next_usage_poll() == 900


async def test_pictures_only_for_camera_states(tmp_path):
    archive, ha, r2, _, clock = build(tmp_path, camera_states=["triggered"])
    ha.state_list = [{"entity_id": PANEL, "state": "disarmed", "attributes": {}, "last_changed": "t"}]
    when = datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC)
    clock.moment = when - timedelta(minutes=10)
    async with running(archive.follow_panel(), archive.upload_loop()):
        await settle(lambda: archive.state.data["panel_cursor"] is not None)
        await ha.panel_events.put(panel_event("triggered", "disarmed", when))
        await ha.panel_events.put(panel_event("armed_night", "triggered", when - timedelta(seconds=30)))
        await settle(lambda: len(r2.objects) == 5)
    disarmed = "snapshots/2026/10/31/20261031T101733.000000Z_disarmed/"
    triggered = "snapshots/2026/10/31/20261031T101703.000000Z_triggered/"
    assert sorted(k for k in r2.objects if k.startswith(disarmed)) == [disarmed + "meta.json",
                                                                      disarmed + "zustand.json.gz"]
    meta = json.loads(r2.objects[disarmed + "meta.json"])
    assert meta["cameras"] == {} and meta["missing_cameras"] == []
    assert triggered + "kamera_front.jpg" in r2.objects


async def test_stopping_logs_no_warning(tmp_path, caplog):
    archive, ha, _, _, _ = build(tmp_path)

    async def connect():
        pass

    async def config():
        return {"time_zone": "Europe/Berlin"}

    async def wait_closed():
        await asyncio.Event().wait()

    async def close():
        pass

    ha.connect, ha.config, ha.wait_closed, ha.close = connect, config, wait_closed, close
    archive.cfg = make_config(tmp_path, alarm_panel="")
    task = asyncio.create_task(archive.ha_loop())
    await settle(lambda: archive.ha_connected)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert archive.ha_connected is False


async def test_picture_series(tmp_path, monkeypatch):
    archive, ha, r2, _, clock = build(tmp_path, camera_pictures=3, camera_interval=1)
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)
    ha.state_list = [{"entity_id": PANEL, "state": "triggered", "attributes": {}, "last_changed": "t"}]
    when = datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC)
    await archive.snapshot(when, "armed_night", "triggered", {}, None, reconstructed=False)
    await asyncio.gather(*archive._series)
    folder = "snapshots/2026/10/31/20261031T101733.000000Z_triggered/"
    keys = sorted(i.key for i in archive.queue.items())
    assert keys == [folder + n for n in ("kamera_front.jpg", "kamera_front_2.jpg", "kamera_front_3.jpg",
                                         "meta.json", "zustand.json.gz")]
    meta = json.loads(archive.queue.body(next(i for i in archive.queue.items() if i.key.endswith("meta.json"))))
    assert meta["cameras"] == {"camera.front": "kamera_front.jpg"}
    assert meta["series"] == {"camera.front": ["kamera_front.jpg", "kamera_front_2.jpg", "kamera_front_3.jpg"]}
    assert sleeps == [1, 1]


async def test_index_is_read_from_r2_when_empty(tmp_path):
    archive, _, r2, _, _ = build(tmp_path)
    archive._restored.clear()
    folder = "snapshots/2026/10/30/20261030T200000.000000Z_triggered/"
    r2.objects.update({"protokoll/2026/10/30/a.json": b"a", "protokoll/2026/10/31/b.json": b"b",
                       folder + "meta.json": b"{}", "zusammenfassung/2026/10/2026-10-30.txt": b"s",
                       "zusammenfassung/2026/10/2026-10-30-nachtrag-1.txt": b"s"})
    await archive.restore_index()
    assert archive._restored.is_set()
    data = archive.state.data
    assert sorted(data["uploaded"]) == ["2026-10-30", "2026-10-31"]
    assert data["summaries"]["2026-10-30"]["files"] == 2
    assert folder in data["summaries"]["2026-10-30"]["covered"]
    # Days without anything get no summary afterwards, as with the first start
    assert data["installed"] is None
    # The 30th needs no new summary, the 31st does
    assert archive._summary_days(datetime(2026, 11, 1).date()) == ["2026-10-31"]
    # An index there already: nothing is listed again
    r2.calls.clear()
    archive._restored.clear()
    await archive.restore_index()
    assert archive._restored.is_set() and not r2.calls


async def test_restore_waits_for_r2(tmp_path):
    archive, _, r2, _, _ = build(tmp_path)
    archive._restored.clear()
    r2.fail = R2Error("LIST: unreachable")
    task = asyncio.create_task(archive.restore_index())
    await settle(lambda: archive.r2_connected is False)
    r2.fail = None
    await settle(archive._restored.is_set)
    await task


async def test_quarantine_is_a_problem(tmp_path):
    archive, _, _, _, _ = build(tmp_path)
    archive.ha_connected = True
    archive.queue.quarantine.mkdir(parents=True)
    (archive.queue.quarantine / "0000000007.json").write_text("kaputt")
    (archive.queue.quarantine / "0000000007.bin").write_bytes(b"x")
    assert archive.health() == ["1 objects in quarantine"]
    for path in archive.queue.quarantine.iterdir():
        path.unlink()
    assert archive.health() == []


async def test_token_expiry(tmp_path):
    archive, _, _, cf, clock = build(tmp_path)
    assert archive.status()["token_expiring"] is None
    cf.expiry = clock() + timedelta(days=20)
    task = asyncio.create_task(archive.token_loop())
    await settle(lambda: archive.token_checked)
    task.cancel()
    assert archive.status()["token_expiring"] is False
    assert json.loads(archive.status()["token_expiring/attributes"])["expires_on"] == cf.expiry.isoformat()
    clock.moment += timedelta(days=7)
    assert archive.status()["token_expiring"] is True
    archive.token_expiry = None
    assert archive.status()["token_expiring"] is False


async def test_connection_check(tmp_path, monkeypatch):
    archive, _, r2, _, _ = build(tmp_path, r2_check_interval=5)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)
    assert archive.r2_connected is None
    with contextlib.suppress(asyncio.CancelledError):
        await archive.check_loop()
    assert archive.r2_connected is True and sleeps == [300, 300, 300]
    assert archive.guard.own["class_b"] == 3
    # Unreachable: off; refused key: still connected
    sleeps.clear()
    r2.fail = R2Error("HEAD: timeout")
    with contextlib.suppress(asyncio.CancelledError):
        await archive.check_loop()
    assert archive.r2_connected is False
    sleeps.clear()
    r2.fail = R2Error("HEAD: HTTP 403", 403, "AccessDenied")
    with contextlib.suppress(asyncio.CancelledError):
        await archive.check_loop()
    assert archive.r2_connected is True
    # Without a usage figure: no check, again in a minute
    sleeps.clear()
    archive.guard.usage = None
    with contextlib.suppress(asyncio.CancelledError):
        await archive.check_loop()
    assert sleeps == [60, 60, 60]
