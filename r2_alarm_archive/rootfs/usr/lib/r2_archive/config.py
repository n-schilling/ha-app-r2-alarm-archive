"""App options from /data/options.json, and the MQTT broker the Supervisor knows."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .archive import PANEL_STATES

OPTIONS = Path("/data/options.json")
ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# The three kinds of data, each under its own prefix in the bucket
KINDS = ("log", "snapshots", "summaries")


class ConfigError(Exception):
    """The options are incomplete or invalid."""


@dataclass(frozen=True)
class Config:
    account_id: str
    bucket: str
    jurisdiction: str              # "default" or "eu"
    access_key_id: str
    secret_access_key: str
    api_token: str | None
    free_tier_guard: bool
    log_entity: str
    alarm_panel: str | None
    snapshot_states: tuple[str, ...]
    snapshot_entities: tuple[str, ...]
    snapshot_all_entities: bool
    cameras: tuple[str, ...]
    camera_states: tuple[str, ...]     # snapshots that also get pictures
    camera_pictures: int               # pictures per camera and snapshot, a series
    camera_interval: int               # seconds between the pictures of a series
    r2_check_interval: int             # minutes between checks of the R2 connection
    retention: dict[str, int | None]   # kind -> days, None = unlimited
    lock: dict[str, int | str | None]  # kind -> days, "indefinite", None = off
    # The broker from the options; None: ask the Supervisor (see supervisor_mqtt)
    mqtt: dict | None
    discovery_prefix: str
    log_level: str


class BrokerUnavailable(Exception):
    """The Supervisor knows no MQTT broker (yet)."""


def supervisor_mqtt() -> dict:
    """The broker the Supervisor knows, e.g. the Mosquitto broker app."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BrokerUnavailable("no MQTT broker set and no Supervisor to ask for one")
    req = urllib.request.Request("http://supervisor/services/mqtt",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)["data"]
    except (OSError, ValueError, KeyError) as err:
        raise BrokerUnavailable(f"no MQTT broker available from the Supervisor ({err}); "
                                "install the Mosquitto broker app or set mqtt_host") from err
    return {"host": data["host"], "port": int(data.get("port") or 1883),
            "username": data.get("username") or None, "password": data.get("password") or None}


def _days(name: str, raw: str, words: dict[str, int | str | None]) -> int | str | None:
    text = str(raw).strip().lower()
    if text in words:
        return words[text]
    if text.isdigit() and 0 < int(text) < 100000:
        return int(text)
    raise ConfigError(f"{name} must be {' or '.join(words)} or a number of days, not {raw!r}")


def _entities(name: str, raw: object) -> tuple[str, ...]:
    values = tuple(str(v).strip() for v in (raw or []) if str(v).strip())
    if bad := [v for v in values if not ENTITY_ID.match(v)]:
        raise ConfigError(f"{name}: {', '.join(bad)} is no entity ID")
    return values


def _number(name: str, raw: object, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as err:
        raise ConfigError(f"{name} must be a number from {low} to {high}, not {raw!r}") from err
    if not low <= value <= high:
        raise ConfigError(f"{name} must be a number from {low} to {high}, not {value}")
    return value


def load(path: Path = OPTIONS) -> Config:
    try:
        opts = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise ConfigError(f"{path} not readable: {err}") from err

    missing = [k for k in ("account_id", "bucket", "access_key_id", "secret_access_key",
                           "log_entity") if not str(opts.get(k) or "").strip()]
    if missing:
        raise ConfigError(f"set {', '.join(missing)} on the app's Configuration tab")
    account_id = str(opts["account_id"]).strip().lower()
    if not ACCOUNT_ID.match(account_id):
        raise ConfigError("account_id must be the 32 hex digits of the Cloudflare account ID")
    bucket = str(opts["bucket"]).strip()
    if not BUCKET.match(bucket):
        raise ConfigError(f"bucket {bucket!r} is no R2 bucket name")
    jurisdiction = str(opts.get("jurisdiction") or "default")
    if jurisdiction not in ("default", "eu"):
        raise ConfigError("jurisdiction must be default or eu")
    log_entity = _entities("log_entity", [opts["log_entity"]])[0]
    panel = _entities("alarm_panel", [opts.get("alarm_panel")])
    if panel and not panel[0].startswith("alarm_control_panel."):
        raise ConfigError("alarm_panel must be an alarm_control_panel entity")
    states = tuple(opts.get("snapshot_states") or ())
    if bad := [s for s in states if s not in PANEL_STATES]:
        raise ConfigError(f"snapshot_states: {', '.join(bad)} is no state of an alarm panel")
    cameras = _entities("cameras", opts.get("cameras"))
    if bad := [c for c in cameras if not c.startswith("camera.")]:
        raise ConfigError(f"cameras: {', '.join(bad)} is no camera entity")
    camera_states = tuple(opts.get("camera_states") or ())
    if bad := [s for s in camera_states if s not in states]:
        raise ConfigError(f"camera_states: {', '.join(bad)} is not in snapshot_states; "
                          "pictures are taken only with a snapshot")
    camera_pictures = _number("camera_pictures", opts.get("camera_pictures", 1), 1, 10)
    camera_interval = _number("camera_interval", opts.get("camera_interval", 10), 1, 60)
    r2_check_interval = _number("r2_check_interval", opts.get("r2_check_interval", 15), 1, 1440)

    retention = {k: _days(f"retention_{k}", opts.get(f"retention_{k}", "unlimited"),
                          {"unlimited": None}) for k in KINDS}
    lock = {k: _days(f"lock_{k}", opts.get(f"lock_{k}", "off"),
                     {"off": None, "indefinite": "indefinite"}) for k in KINDS}
    for kind in KINDS:
        if lock[kind] is not None and retention[kind] is not None and \
                (lock[kind] == "indefinite" or lock[kind] > retention[kind]):
            raise ConfigError(f"lock_{kind} keeps objects longer than retention_{kind} deletes "
                              "them; the lock would win, so set retention to at least the lock")

    api_token = str(opts.get("api_token") or "").strip() or None
    guard = bool(opts.get("free_tier_guard", True))
    if guard and not api_token:
        raise ConfigError("free_tier_guard needs api_token (Account Analytics Read and "
                          "Workers R2 Storage Read); or turn the guard off")

    mqtt = None
    if opts.get("mqtt_host"):
        mqtt = {"host": str(opts["mqtt_host"]), "port": int(opts.get("mqtt_port") or 1883),
                "username": opts.get("mqtt_username") or None,
                "password": opts.get("mqtt_password") or None}

    return Config(
        account_id=account_id,
        bucket=bucket,
        jurisdiction=jurisdiction,
        access_key_id=str(opts["access_key_id"]).strip(),
        secret_access_key=str(opts["secret_access_key"]).strip(),
        api_token=api_token,
        free_tier_guard=guard,
        log_entity=log_entity,
        alarm_panel=panel[0] if panel else None,
        snapshot_states=states,
        snapshot_entities=_entities("snapshot_entities", opts.get("snapshot_entities")),
        snapshot_all_entities=bool(opts.get("snapshot_all_entities", False)),
        cameras=cameras,
        camera_states=camera_states,
        camera_pictures=camera_pictures,
        camera_interval=camera_interval,
        r2_check_interval=r2_check_interval,
        retention=retention,
        lock=lock,
        mqtt=mqtt,
        discovery_prefix=opts.get("discovery_prefix") or "homeassistant",
        log_level=str(opts.get("log_level") or "info").upper(),
    )
