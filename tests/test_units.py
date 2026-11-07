"""Options, the free tier guard, keys and summaries, the bucket rules and the
queue in /data."""

import json
from datetime import UTC, datetime

import pytest

from conftest import BERLIN, make_config, options, unzip
from r2_archive import archive as a
from r2_archive import config as cfg_module
from r2_archive import discovery as d
from r2_archive.cloudflare import Usage, parse_usage
from r2_archive.guard import LIMITS, Guard
from r2_archive.store import Queue, State

MONTH = (2026, 10)


# --- Options --------------------------------------------------------------------
def test_options(cfg):
    assert cfg.retention == {"log": None, "snapshots": 365, "summaries": None}
    assert cfg.lock == {"log": None, "snapshots": None, "summaries": None}
    assert cfg.mqtt_host == "localhost" and cfg.free_tier_guard


@pytest.mark.parametrize("extra, message", [
    ({"bucket": ""}, "set bucket"),
    ({"account_id": "xyz"}, "32 hex digits"),
    ({"bucket": "Alarm_Archive"}, "no R2 bucket name"),
    ({"log_entity": "Alarm Protokoll"}, "no entity ID"),
    ({"alarm_panel": "sensor.x"}, "alarm_control_panel"),
    ({"cameras": ["image.x"]}, "no camera entity"),
    ({"retention_log": "forever"}, "retention_log must be"),
    ({"lock_log": "7", "retention_log": "3"}, "lock would win"),
    ({"lock_snapshots": "indefinite"}, "lock would win"),
    ({"camera_states": ["armed_home"]}, "not in snapshot_states"),
    ({"api_token": ""}, "free_tier_guard needs api_token"),
])
def test_bad_options(tmp_path, extra, message):
    with pytest.raises(cfg_module.ConfigError, match=message):
        make_config(tmp_path, **extra)


def test_guard_off_needs_no_token(tmp_path):
    cfg = make_config(tmp_path, api_token="", free_tier_guard=False, alarm_panel="")
    assert cfg.api_token is None and cfg.alarm_panel is None


def test_options_not_readable(tmp_path):
    with pytest.raises(cfg_module.ConfigError, match="not readable"):
        cfg_module.load(tmp_path / "missing.json")
    path = tmp_path / "o.json"
    path.write_text(json.dumps({**options(), "mqtt_host": ""}))
    assert cfg_module.load(path, mqtt_service=lambda: {"host": "core-mosquitto", "port": 1883}).mqtt_host \
        == "core-mosquitto"


# --- Guard ----------------------------------------------------------------------
def test_guard_needs_a_recent_figure():
    guard = Guard(enabled=True)
    assert not guard.allows(0, MONTH, class_a=1)
    assert guard.limit_reached(0, MONTH) is None
    guard.update(Usage(0, 0, 0), now=100, month=MONTH)
    assert guard.allows(100, MONTH, class_a=1)
    assert not guard.allows(100 + 3601, MONTH, class_a=1)       # too old
    assert not guard.allows(100, (2026, 11), class_a=1)         # last month's figure


def test_guard_stays_ten_percent_below():
    guard = Guard(enabled=True)
    guard.update(Usage(LIMITS["class_a"] - 2, 0, 0), now=0, month=MONTH)
    assert LIMITS == {"class_a": 900_000, "class_b": 9_000_000, "storage": 9_000_000_000}
    assert guard.allows(0, MONTH, class_a=1)
    guard.count(class_a=1)
    assert not guard.allows(0, MONTH, class_a=1)
    assert guard.limit_reached(0, MONTH) is True
    guard.update(Usage(0, 0, LIMITS["storage"] - 10), now=0, month=MONTH)
    assert guard.allows(0, MONTH, storage=9) and not guard.allows(0, MONTH, storage=10)
    assert guard.percent()["storage"] == pytest.approx(90.0)


def test_guard_off_allows_everything():
    guard = Guard(enabled=False)
    assert guard.allows(0, MONTH, class_a=10**9) and guard.limit_reached(0, MONTH) is False


def test_parse_usage():
    data = {"data": {"viewer": {"accounts": [{
        "operations": [{"sum": {"requests": 5}, "dimensions": {"actionType": "PutObject"}},
                       {"sum": {"requests": 2}, "dimensions": {"actionType": "ListObjects"}},
                       {"sum": {"requests": 7}, "dimensions": {"actionType": "GetObject"}},
                       {"sum": {"requests": 9}, "dimensions": {"actionType": "DeleteObject"}}],
        "storage": [{"max": {"payloadSize": 100, "metadataSize": 1}, "dimensions": {"bucketName": "a"}},
                    {"max": {"payloadSize": 50, "metadataSize": 0}, "dimensions": {"bucketName": "b"}}],
    }]}}}
    assert parse_usage(data) == Usage(7, 7, 151)
    for group in data["data"]["viewer"]["accounts"][0]["operations"]:
        group["dimensions"]["bucketName"] = "eu_a" if group["dimensions"]["actionType"] == "PutObject" else "b"
    assert parse_usage(data, {"a", "eu_a"}) == Usage(7, 7, 151, 5, 0, 101)


# --- Keys, objects, summaries ----------------------------------------------------
RAW = {"when": 1793403762.5, "name": "Alarm", "message": "Nacht scharf geschaltet",
       "entity_id": "input_boolean.alarm_log", "context_user_id": "u1"}


def test_log_entry_filter():
    assert a.log_entry(RAW, "input_boolean.alarm_log")["message"].startswith("Nacht")
    assert a.log_entry({**RAW, "state": "on"}, "input_boolean.alarm_log") is None
    assert a.log_entry({**RAW, "entity_id": "light.x"}, "input_boolean.alarm_log") is None
    assert a.log_entry({**RAW, "message": ""}, "input_boolean.alarm_log") is None


def test_log_key_is_stable_and_local():
    entry = a.log_entry(RAW, "input_boolean.alarm_log")
    key, day, body = a.log_object(entry, BERLIN)
    assert (key, day) == a.log_object(entry, BERLIN)[:2]
    # 23:42 UTC on 30.10. is 00:42 on 31.10. in Berlin (winter time since 25.10.)
    assert datetime.fromtimestamp(RAW["when"], UTC).strftime("%d %H:%M") == "30 23:42"
    assert day == "2026-10-31"
    assert key.startswith("protokoll/2026/10/31/20261030T234242.500000Z_") and key.endswith(".json")
    assert json.loads(body) == {"time": "2026-10-30T23:42:42.500000+00:00", "name": "Alarm",
                                "message": RAW["message"], "user_id": "u1"}
    other = a.log_object({**entry, "message": "anders"}, BERLIN)[0]
    assert other != key


def test_snapshot_bodies():
    moment = datetime(2026, 10, 31, 10, 17, 33, tzinfo=UTC)
    folder, day = a.snapshot_folder(moment, "triggered", BERLIN)
    assert folder == "snapshots/2026/10/31/20261031T101733.000000Z_triggered/" and day == "2026-10-31"
    assert a.camera_file("camera.front_medium") == "kamera_front_medium.jpg"
    states = unzip(a.states_body(moment, [{"entity_id": "x", "state": "on"}], True))
    assert states["reconstructed"] and states["states"][0]["entity_id"] == "x"
    meta = json.loads(a.meta_body(moment, "alarm_control_panel.z", "armed_night", "triggered", {},
                                  None, {"camera.a": "kamera_a.jpg", "camera.b": None}, False))
    assert meta["cameras"] == {"camera.a": "kamera_a.jpg"} and meta["missing_cameras"] == ["camera.b"]
    assert meta["reason"] is None
    folder, _ = a.snapshot_folder(moment, a.REQUEST, BERLIN)
    assert folder == "snapshots/2026/10/31/20261031T101733.000000Z_request/"
    meta = json.loads(a.meta_body(moment, None, None, a.REQUEST, {}, None, {}, False,
                                  reason="Guest opened the study"))
    assert meta["to"] == "request" and meta["reason"] == "Guest opened the study"


def test_request_reason():
    assert a.request_reason(None) is None
    assert a.request_reason("   ") is None
    assert a.request_reason(" Guest\n opened  the study ") == "Guest opened the study"
    assert a.request_reason(42) == "42"
    assert a.request_reason("y" * 300) == "y" * 200


def test_summary_text():
    entries = [("protokoll/2026/10/31/b.json", json.dumps({"time": "2026-10-31T10:17:44+00:00",
                                                            "message": "Manuell entschärft"}).encode()),
               ("protokoll/2026/10/31/a.json", json.dumps({"time": "2026-10-31T10:17:33+00:00",
                                                            "message": "ALARM ausgelöst"}).encode())]
    text = a.summary_text("2026-10-31", entries, ["snapshots/2026/10/31/x/"], BERLIN, 0,
                          datetime(2026, 11, 1, 0, 5, tzinfo=UTC)).decode()
    lines = text.splitlines()
    assert lines[0] == "Alarm-Protokoll 31.10.2026"
    assert lines[3:5] == ["31.10.2026 11:17:33 - ALARM ausgelöst", "31.10.2026 11:17:44 - Manuell entschärft"]
    assert "  snapshots/2026/10/31/x/" in lines and "2 Einträge, erstellt 2026-11-01T01:05:00+01:00" in text
    assert a.summary_key("2026-10-31", 0) == "zusammenfassung/2026/10/2026-10-31.txt"
    assert a.summary_key("2026-10-31", 2) == "zusammenfassung/2026/10/2026-10-31-nachtrag-2.txt"
    empty = a.summary_text("2026-10-30", [], [], BERLIN, 1, datetime(2026, 11, 1, tzinfo=UTC)).decode()
    assert "Nachtrag 1" in empty and "(keine Einträge)" in empty


# --- Bucket rules ---------------------------------------------------------------
def lifecycle(prefix, days, enabled=True):
    return {"id": f"del-{prefix}", "enabled": enabled, "conditions": {"prefix": prefix},
            "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": days * 86400}}}


def lock(prefix, days=None):
    condition = {"type": "Indefinite"} if days is None else {"type": "Age", "maxAgeSeconds": days * 86400}
    return {"id": f"lock-{prefix}", "enabled": True, "prefix": prefix, "condition": condition}


RETENTION = {"log": None, "snapshots": 365, "summaries": None}
NO_LOCK = {"log": None, "snapshots": None, "summaries": None}


def test_rules_match():
    assert a.rule_problems([lifecycle("snapshots/", 365)], [], RETENTION, NO_LOCK) == []
    assert a.rule_problems([lifecycle("snapshots/", 365)], [lock("protokoll/")], RETENTION,
                           {**NO_LOCK, "log": "indefinite"}) == []
    assert a.rule_problems([lifecycle("snapshots/", 365), lifecycle("protokoll/", 1, enabled=False)],
                           [lock("snapshots/", 30)], RETENTION, {**NO_LOCK, "snapshots": 30}) == []


def test_rules_differ():
    missing = a.rule_problems([], [], RETENTION, NO_LOCK)
    assert len(missing) == 1 and missing[0].startswith("snapshots/: retention 365 days")
    # A rule without prefix covers every kind
    everything = a.rule_problems([lifecycle("", 365)], [], RETENTION, NO_LOCK)
    assert [p.split(":")[0] for p in everything] == ["protokoll/", "zusammenfassung/"]
    wrong_lock = a.rule_problems([lifecycle("snapshots/", 365)], [lock("", 7)], RETENTION, NO_LOCK)
    assert len(wrong_lock) == 3
    assert a.rule_problems([lifecycle("snapshots/", 365)], [lock("protokoll/", 7)], RETENTION,
                           {**NO_LOCK, "log": "indefinite"})[0].startswith("protokoll/: lock indefinite")


def test_rule_commands_name_the_jurisdiction():
    assert a.rule_commands("b", RETENTION, NO_LOCK, "eu") == [
        "npx wrangler r2 bucket lifecycle add b delete-snapshots snapshots/ --expire-days 365 --jurisdiction eu"]


def test_rule_commands():
    assert a.rule_commands("b", RETENTION, {**NO_LOCK, "log": "indefinite", "snapshots": 30}) == [
        "npx wrangler r2 bucket lock add b lock-log protokoll/ --retention-indefinite",
        "npx wrangler r2 bucket lifecycle add b delete-snapshots snapshots/ --expire-days 365",
        "npx wrangler r2 bucket lock add b lock-snapshots snapshots/ --retention-days 30",
    ]


# --- Queue and state ---------------------------------------------------------------
def test_queue_survives_a_restart(tmp_path):
    queue = Queue(tmp_path / "q")
    assert queue.add("k1", "log", "2026-10-31", b"one", "application/json")
    assert not queue.add("k1", "log", "2026-10-31", b"again", "application/json")
    queue.add("k2", "snapshots", "2026-10-30", b"two", "image/jpeg")
    # A body without meta file: the crash came before the item existed
    (tmp_path / "q" / "0000000099.bin").write_bytes(b"half")
    (tmp_path / "q" / ".0000000100.json.tmp").write_bytes(b"{")
    again = Queue(tmp_path / "q")
    assert [i.key for i in again.items()] == ["k1", "k2"] and "k2" in again
    assert again.body(again.items()[1]) == b"two" and again.days() == {"2026-10-31", "2026-10-30"}
    assert not (tmp_path / "q" / "0000000099.bin").exists()
    again.done(again.items()[0])
    assert [i.key for i in Queue(tmp_path / "q").items()] == ["k2"]
    assert again.add("k3", "log", "2026-10-31", b"three", "text/plain")
    assert [i.seq for i in again.items()] == [2, 3]
    assert again.oldest() == min(i.queued for i in again.items())
    assert Queue(tmp_path / "empty").oldest() is None


def test_state(tmp_path):
    state = State(tmp_path / "state.json")
    state.uploaded("2026-10-31", "k")
    state.uploaded("2026-10-31", "k")
    state.data["log_cursor"] = 1.5
    state.save()
    loaded = State(tmp_path / "state.json")
    assert loaded.data["uploaded"] == {"2026-10-31": ["k"]} and loaded.data["log_cursor"] == 1.5
    (tmp_path / "broken.json").write_text("{")
    assert State(tmp_path / "broken.json").data["summaries"] == {}


def test_discovery():
    payload = d.device_payload(d.Context("homeassistant", "0.1.0", None))
    assert d.device_topic("homeassistant") == "homeassistant/device/r2_alarm_archive/config"
    comps = payload["components"]
    assert set(comps) == {"archiving", "r2_connection", "queue", "last_upload", "last_summary", "free_tier_usage",
                          "free_tier_limit", "bucket_rules", "token_expiring"}
    assert all(c["unique_id"].startswith("r2_alarm_archive_") for c in comps.values())
    assert comps["r2_connection"]["device_class"] == "connectivity"
    assert comps["archiving"]["device_class"] == "running"
    assert payload["availability"] == [{"topic": "r2_alarm_archive/availability"}]


def test_unreadable_queue_entry_is_kept_aside(tmp_path):
    kept = tmp_path / "public" / "quarantine"
    queue = Queue(tmp_path / "q", kept)
    queue.add("k1", "log", "2026-10-31", b"one", "application/json")
    (tmp_path / "q" / "0000000001.json").write_text('{"key": "k1"}')
    again = Queue(tmp_path / "q", kept)
    assert len(again) == 0
    assert (kept / "0000000001.json").exists() and (kept / "0000000001.bin").read_bytes() == b"one"
    assert [q.name for q in again.quarantined()] == ["0000000001.bin", "0000000001.json"]
    # Deleted by hand: gone
    for path in kept.iterdir():
        path.unlink()
    assert again.quarantined() == []
    assert Queue(tmp_path / "q").quarantined() == []


def test_key_parts():
    assert a.key_day("protokoll/2026/10/31/x.json") == "2026-10-31"
    assert a.key_day("snapshots/2026/10/31/f/meta.json") == "2026-10-31"
    assert a.key_day("protokoll/kaputt") is None
    assert a.summary_part("zusammenfassung/2026/10/2026-10-31.txt") == ("2026-10-31", 0)
    assert a.summary_part("zusammenfassung/2026/10/2026-10-31-nachtrag-2.txt") == ("2026-10-31", 2)
    assert a.summary_part("zusammenfassung/2026/10/notiz.txt") is None
    assert a.camera_file("camera.front") == "kamera_front.jpg"
    assert a.camera_file("camera.front", 3) == "kamera_front_3.jpg"


@pytest.mark.parametrize("extra, message", [({"camera_pictures": 0}, "camera_pictures"),
                                            ({"camera_interval": "x"}, "camera_interval")])
def test_series_options(tmp_path, extra, message):
    with pytest.raises(cfg_module.ConfigError, match=message):
        make_config(tmp_path, **extra)
