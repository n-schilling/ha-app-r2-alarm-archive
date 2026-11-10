"""Runs the archive and, independent of it, the MQTT connection that shows
its status in Home Assistant: the archive keeps working while the broker is
gone."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

import aiohttp
import aiomqtt

from . import config as cfg_module
from . import discovery as d
from .app import BACKOFF, Archive
from .cloudflare import Cloudflare
from .ha import HA
from .r2 import R2
from .store import Queue, State
from .web import Cache, Web

LOGGER = logging.getLogger("r2_archive")
DATA = Path("/data")
# The app's folder in /addon_configs, which the user can reach
PUBLIC = Path("/config")
# The MQTT payload Home Assistant shows as unknown
UNKNOWN = "None"


def payload(value: object) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    return str(value)


async def find_broker(cfg: cfg_module.Config, lookup=cfg_module.supervisor_mqtt) -> dict:
    """The broker from the options, else the one the Supervisor knows. Without
    one it asks again and again: the archive works on meanwhile, only its
    status does not reach Home Assistant."""
    if cfg.mqtt:
        return cfg.mqtt
    failures = 0
    while True:
        try:
            return await asyncio.to_thread(lookup)
        except cfg_module.BrokerUnavailable as err:
            if not failures:
                LOGGER.warning("%s; archiving goes on, the status entities wait for a broker", err)
            await asyncio.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)])
            failures += 1


async def mqtt_loop(cfg: cfg_module.Config, archive: Archive, ctx: d.Context, slug: str) -> None:
    will = aiomqtt.Will(d.AVAILABILITY, "offline", qos=1, retain=True)
    failures = 0
    while True:
        broker = await find_broker(cfg)
        try:
            async with aiomqtt.Client(broker["host"], broker["port"], username=broker["username"],
                                      password=broker["password"], will=will,
                                      identifier=f"{slug}-archive") as mqtt:
                LOGGER.info("Connected to MQTT %s:%d", broker["host"], broker["port"])
                failures = 0
                await mqtt.publish(d.device_topic(cfg.discovery_prefix),
                                   json.dumps(d.device_payload(ctx)), qos=1, retain=True)
                await mqtt.publish(d.AVAILABILITY, "online", qos=1, retain=True)
                sent: dict[str, str] = {}
                try:
                    while True:
                        archive.changed.clear()
                        for name, value in archive.status().items():
                            text = payload(value)
                            if sent.get(name) != text:
                                await mqtt.publish(d.topic(name), text, qos=1, retain=True)
                                sent[name] = text
                        # Also every minute: the guard's figure ages without an event
                        try:
                            async with asyncio.timeout(60):
                                await archive.changed.wait()
                        except TimeoutError:
                            pass
                finally:
                    # A regular stop: the last will only covers a dying app
                    try:
                        await asyncio.shield(mqtt.publish(d.AVAILABILITY, "offline", qos=1, retain=True))
                    except aiomqtt.MqttError:
                        pass
        except aiomqtt.MqttError as err:
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            LOGGER.warning("MQTT: %s; connecting again in %d s", err, delay)
            await asyncio.sleep(delay)


async def run(cfg: cfg_module.Config) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise cfg_module.ConfigError("no SUPERVISOR_TOKEN: the app needs homeassistant_api")
    slug = os.environ.get("HOSTNAME", "r2-alarm-archive").replace("-", "_")
    version = os.environ.get("APP_VERSION") or "unknown"
    ctx = d.Context(prefix=cfg.discovery_prefix, version=version,
                    config_url=f"homeassistant://config/app/{slug}/info")
    async with aiohttp.ClientSession() as http:
        cloudflare = Cloudflare(http, cfg.api_token, cfg.account_id, cfg.bucket, cfg.jurisdiction) \
            if cfg.api_token else None
        archive = Archive(cfg, HA(http, token),
                          R2(http, cfg.account_id, cfg.jurisdiction, cfg.bucket, cfg.access_key_id,
                             cfg.secret_access_key),
                          cloudflare, Queue(DATA / "queue", PUBLIC / "quarantine"), State(DATA / "state.json"))
        LOGGER.info("Archiving %s to bucket %s; %d objects waiting from before", cfg.log_entity,
                    cfg.bucket, len(archive.queue))
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(mqtt_loop(cfg, archive, ctx, slug))
            tasks.create_task(archive.restore_index())
            tasks.create_task(archive.ha_loop())
            tasks.create_task(archive.upload_loop())
            tasks.create_task(archive.check_loop())
            tasks.create_task(archive.usage_loop())
            tasks.create_task(archive.rules_loop())
            tasks.create_task(archive.token_loop())
            tasks.create_task(archive.summary_loop())
            tasks.create_task(Web(archive, Cache(DATA / "cache"), slug=slug, version=version).run())


def main() -> None:
    try:
        cfg = cfg_module.load()
    except cfg_module.ConfigError as err:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.error("%s", err)
        sys.exit(1)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("aiomqtt").setLevel(logging.WARNING)

    async def main_task() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await run(cfg)
        except asyncio.CancelledError:
            LOGGER.info("Stopped")

    LOGGER.info("R2 Alarm Archive %s", os.environ.get("APP_VERSION", ""))
    try:
        asyncio.run(main_task())
    except* cfg_module.ConfigError as group:
        LOGGER.error("%s", group.exceptions[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
