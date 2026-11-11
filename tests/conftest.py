"""Puts the archive package on the path and provides the shared fakes."""

import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "r2_alarm_archive/rootfs/usr/lib"))

from r2_archive import config as cfg_module  # noqa: E402
from r2_archive.cloudflare import Usage  # noqa: E402
from r2_archive.r2 import R2Error  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
ACCOUNT = "0123456789abcdef0123456789abcdef"


def options(**extra):
    opts = {"account_id": ACCOUNT, "bucket": "alarm-archive", "jurisdiction": "default",
            "access_key_id": "AKID", "secret_access_key": "SECRET", "api_token": "TOKEN",
            "free_tier_guard": True, "log_entity": "input_boolean.alarm_log",
            "alarm_panel": "alarm_control_panel.home",
            "snapshot_states": ["armed_away", "armed_night", "triggered", "disarmed"],
            "snapshot_entities": ["binary_sensor.doors_and_windows"], "snapshot_all_entities": False,
            "cameras": ["camera.front"], "camera_states": ["armed_night", "triggered"], "mqtt_host": "localhost"}
    opts.update(extra)
    return opts


def make_config(tmp_path, **extra):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options(**extra)))
    return cfg_module.load(path)


class FakeR2:
    """Stands in for R2: objects by key, PUT only once, optional failures."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail: Exception | None = None
        self.calls: list[tuple[str, str]] = []

    async def put_once(self, key, body, content_type):
        self.calls.append(("PUT", key))
        if self.fail:
            raise self.fail
        if key in self.objects:
            return False
        self.objects[key] = body
        return True

    async def get(self, key):
        self.calls.append(("GET", key))
        if self.fail:
            raise self.fail
        if key not in self.objects:
            raise R2Error(f"GET {key}: HTTP 404", 404, "NoSuchKey")
        return self.objects[key]

    async def check(self):
        self.calls.append(("HEAD", ""))
        if self.fail:
            raise self.fail

    async def list(self, prefix):
        self.calls.append(("LIST", prefix))
        if self.fail:
            raise self.fail
        return sorted(k for k in self.objects if k.startswith(prefix))


class FakeHA:
    """Stands in for Home Assistant: states, history, cameras and three
    subscriptions fed from queues."""

    def __init__(self):
        self.state_list: list[dict] = []
        self.users: list[dict] = []
        self.history_data: dict[str, list[dict]] = {}
        self.history_calls: list[tuple] = []
        self.pictures: dict[str, bytes] = {"camera.front": b"\xff\xd8jpeg"}
        self.log_events: asyncio.Queue = asyncio.Queue()
        self.panel_events: asyncio.Queue = asyncio.Queue()
        self.request_events: asyncio.Queue = asyncio.Queue()
        self.log_start: str | None = None

    async def states(self):
        return self.state_list

    async def call(self, message):
        assert message == {"type": "config/auth/list"}
        return self.users

    async def history(self, entity_ids, start, end=None, attributes=True):
        self.history_calls.append((tuple(entity_ids), start, end))
        return {e: self.history_data[e] for e in entity_ids if e in self.history_data}

    async def camera(self, entity_id):
        from r2_archive.ha import HAError
        if entity_id not in self.pictures:
            raise HAError(f"{entity_id}: HTTP 500")
        return self.pictures[entity_id]

    async def logbook(self, entity_id, start):
        self.log_start = start
        while True:
            yield await self.log_events.get()

    async def panel_changes(self, entity_id):
        while True:
            yield await self.panel_events.get()

    async def snapshot_requests(self):
        while True:
            yield await self.request_events.get()


SMALL_USAGE = Usage(10, 100, 1000)


class FakeCloudflare:
    def __init__(self, usage=SMALL_USAGE):
        self.result = usage
        self.lifecycle: list[dict] = []
        self.locks: list[dict] = []
        self.expiry = None

    async def token_expiry(self):
        return self.expiry

    async def usage(self, now):
        return self.result

    async def lifecycle_rules(self):
        return self.lifecycle

    async def lock_rules(self):
        return self.locks


class Clock:
    def __init__(self, moment=datetime(2026, 10, 31, 8, 0, tzinfo=UTC)):
        self.moment = moment

    def __call__(self):
        return self.moment


def unzip(body: bytes) -> dict:
    return json.loads(gzip.decompress(body))


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)
