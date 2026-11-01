"""Home Assistant MQTT discovery for the app's own status entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BASE = "r2_alarm_archive"
AVAILABILITY = f"{BASE}/availability"
SUPPORT_URL = "https://github.com/n-schilling/ha-app-r2-alarm-archive"
NAME = "R2 Alarm Archive"


def topic(name: str) -> str:
    return f"{BASE}/{name}"


@dataclass(frozen=True)
class Context:
    prefix: str
    version: str
    config_url: str | None


def device_topic(prefix: str) -> str:
    return f"{prefix}/device/{BASE}/config"


def _entity(platform: str, uid: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"platform": platform, "name": name, "unique_id": f"{BASE}_{uid}", **extra}


def components() -> dict[str, dict]:
    on_off = {"payload_on": "ON", "payload_off": "OFF"}
    return {
        # The one entity for dashboards: on while the log reaches R2
        "archiving": _entity("binary_sensor", "archiving", "Archiving", device_class="running",
                             state_topic=topic("archiving"),
                             json_attributes_topic=topic("archiving/attributes"), **on_off),
        "r2_connection": _entity("binary_sensor", "r2_connection", "R2 connection",
                                 device_class="connectivity", state_topic=topic("r2_connection"),
                                 **on_off),
        "queue": _entity("sensor", "queue", "Queue", state_topic=topic("queue"),
                         state_class="measurement", icon="mdi:tray-full",
                         unit_of_measurement="objects"),
        "last_upload": _entity("sensor", "last_upload", "Last upload", device_class="timestamp",
                               state_topic=topic("last_upload")),
        "last_summary": _entity("sensor", "last_summary", "Last summary", device_class="timestamp",
                                state_topic=topic("last_summary")),
        "free_tier_usage": _entity("sensor", "free_tier_usage", "Free tier usage",
                                   state_topic=topic("free_tier_usage"), unit_of_measurement="%",
                                   state_class="measurement", icon="mdi:gauge",
                                   json_attributes_topic=topic("free_tier_usage/attributes")),
        "free_tier_limit": _entity("binary_sensor", "free_tier_limit", "Free tier limit reached",
                                   device_class="problem", state_topic=topic("free_tier_limit"),
                                   **on_off),
        "bucket_rules": _entity("binary_sensor", "bucket_rules", "Bucket rules differ",
                                device_class="problem", state_topic=topic("bucket_rules"),
                                json_attributes_topic=topic("bucket_rules/attributes"), **on_off),
        "token_expiring": _entity("binary_sensor", "token_expiring", "API token expires soon",
                                  device_class="problem", state_topic=topic("token_expiring"),
                                  json_attributes_topic=topic("token_expiring/attributes"), **on_off),
    }


def device_payload(ctx: Context) -> dict[str, Any]:
    device: dict[str, Any] = {"identifiers": [BASE], "name": NAME, "manufacturer": "n-schilling",
                              "model": "Cloudflare R2 archive", "sw_version": ctx.version}
    if ctx.config_url:
        device["configuration_url"] = ctx.config_url
    return {
        "device": device,
        "origin": {"name": NAME, "sw_version": ctx.version, "support_url": SUPPORT_URL},
        "availability": [{"topic": AVAILABILITY}],
        "qos": 1,
        "components": components(),
    }
