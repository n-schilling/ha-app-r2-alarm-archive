"""The archive: follows the alarm log and the alarm panel in Home Assistant,
queues what belongs in the bucket, uploads it past the free tier guard and
writes the daily summaries.

Everything that must reach R2 goes through the queue in /data first, so an
outage of R2 or of the network loses nothing. The log stream resumes where
the last queued entry was; panel changes the app missed are rebuilt from
the recorder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time as clock_time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import archive as a
from .cloudflare import Cloudflare, CloudflareError
from .config import Config
from .guard import LIMITS, Guard, utc_month
from .ha import HA, HAError
from .r2 import R2, R2AuthError, R2Error
from .store import Queue, State

LOGGER = logging.getLogger(__name__)

# Retry delays after errors, in seconds
BACKOFF = (5, 15, 60, 300)
USAGE_INTERVAL = 15 * 60
RULES_INTERVAL = 24 * 3600
SUMMARY_AT = time(0, 5)
# The first start reads the log this far back; the recorder keeps less
FIRST_START_DAYS = 400
GROUP_DEPTH = 3
# Archiving counts as not working when an object waits longer than this, or
# when a summary is this late
STUCK_AFTER = 15 * 60
SUMMARY_GRACE = timedelta(hours=1)
# api_token: checked daily, warned about this long before it expires
TOKEN_INTERVAL = 24 * 3600
TOKEN_WARN = timedelta(days=14)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class Archive:
    def __init__(self, cfg: Config, ha: HA, r2: R2, cloudflare: Cloudflare | None, queue: Queue,
                 state: State, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.cfg = cfg
        self.ha = ha
        self.r2 = r2
        self.cloudflare = cloudflare
        self.queue = queue
        self.state = state
        self.now = clock
        self.guard = Guard(enabled=cfg.free_tier_guard)
        self.tz: ZoneInfo | None = None
        self.language = "en"
        self.r2_connected: bool | None = None
        self.ha_connected = False
        self.upload_error: str | None = None      # the last upload failed with this
        self.rule_problems: list[str] | None = None
        self.token_checked = False           # api_token verified at least once
        self.token_expiry: datetime | None = None   # None: it never expires
        self.changed = asyncio.Event()       # a status value changed
        self._wake = asyncio.Event()         # the queue has something new
        self._ready = asyncio.Event()        # the time zone is known
        self._restored = asyncio.Event()     # the index is there, or was read from R2
        self._series: set[asyncio.Task] = set()     # picture series still being taken

    # --- Status ---------------------------------------------------------------
    def health(self) -> list[str]:
        """Why archiving does not work right now; empty while it does: Home
        Assistant connected, uploads succeed, nothing waits too long, the
        guard has a figure below the limit, no summary is overdue."""
        reasons = []
        if not self.ha_connected:
            reasons.append("Home Assistant not connected")
        if self.r2_connected is False:
            reasons.append("R2 not reachable")
        if self.upload_error:
            reasons.append(f"last upload failed: {self.upload_error}")
        if set_aside := {q.name.rsplit(".", 1)[0] for q in self.queue.quarantined()}:
            reasons.append(f"{len(set_aside)} objects in quarantine")
        oldest = self.queue.oldest()
        if oldest is not None and clock_time.time() - oldest > STUCK_AFTER:
            reasons.append(f"{len(self.queue)} objects waiting, the oldest for "
                           f"{int((clock_time.time() - oldest) // 60)} min")
        limit = self.guard.limit_reached(asyncio.get_running_loop().time(), utc_month(self.now()))
        if limit:
            reasons.append("free tier limit reached")
        elif limit is None:
            reasons.append("no R2 usage figure for the free tier guard")
        if self.tz is not None:
            local = self.now().astimezone(self.tz)
            for day in self._summary_days(local.date()):
                due = datetime.combine(date.fromisoformat(day) + timedelta(days=1), SUMMARY_AT, self.tz)
                if local > due + SUMMARY_GRACE:
                    reasons.append(f"summary of {day} missing")
                    break
        return reasons

    def status(self) -> dict[str, object]:
        loop_now = asyncio.get_running_loop().time()
        month = utc_month(self.now())
        percent = self.guard.percent()
        rules: dict[str, object] = {"problems": self.rule_problems or []}
        if self.rule_problems:
            rules["commands"] = a.rule_commands(self.cfg.bucket, self.cfg.retention, self.cfg.lock,
                                                self.cfg.jurisdiction)
        reasons = self.health()
        return {
            "archiving": not reasons,
            "archiving/attributes": json.dumps({"problems": reasons}),
            "r2_connection": self.r2_connected,
            "queue": len(self.queue),
            "last_upload": self.state.data["last_upload"],
            "last_summary": self.state.data["last_summary"],
            "free_tier_usage": max(percent.values()) if percent else None,
            "free_tier_usage/attributes": json.dumps({"percent": percent, "limits": LIMITS,
                                                      "guard": self.cfg.free_tier_guard}),
            "free_tier_limit": self.guard.limit_reached(loop_now, month),
            "bucket_rules": None if self.rule_problems is None else bool(self.rule_problems),
            "bucket_rules/attributes": json.dumps(rules),
            "token_expiring": self.token_expiring(),
            "token_expiring/attributes": json.dumps(
                {"expires_on": iso(self.token_expiry) if self.token_expiry else None}),
        }

    def token_expiring(self) -> bool | None:
        """api_token expires within two weeks; None while not checked."""
        if not self.token_checked:
            return None
        return self.token_expiry is not None and self.token_expiry - self.now() < TOKEN_WARN

    def _changed(self) -> None:
        self.changed.set()

    def _set_r2(self, connected: bool) -> None:
        if self.r2_connected != connected:
            self.r2_connected = connected
            self._changed()

    # --- Queue ------------------------------------------------------------------
    def _done(self, day: str, key: str) -> bool:
        return key in self.state.data["uploaded"].get(day, []) or key in self.queue

    def _enqueue(self, key: str, kind: str, day: str, body: bytes, content_type: str) -> None:
        if not self._done(day, key) and self.queue.add(key, kind, day, body, content_type):
            self._wake.set()
            self._changed()

    async def upload_loop(self) -> None:
        failures = 0
        while True:
            items = self.queue.items()
            if not items:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = items[0]
            loop_now = asyncio.get_running_loop().time()
            if not self.guard.allows(loop_now, utc_month(self.now()), class_a=1, storage=item.size):
                self._changed()
                await self._sleep_or_wake(60)
                continue
            try:
                created = await self.r2.put_once(item.key, self.queue.body(item), item.content_type)
            except R2Error as err:
                self._set_r2(not err.unreachable)
                self.upload_error = str(err)
                self._changed()
                delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
                failures += 1
                level = logging.WARNING if err.unreachable or failures <= 1 else logging.ERROR
                if isinstance(err, R2AuthError):
                    level = logging.ERROR
                LOGGER.log(level, "Upload of %s failed: %s; again in %d s", item.key, err, delay)
                await asyncio.sleep(delay)
                continue
            failures = 0
            self._set_r2(True)
            self.upload_error = None
            self.guard.count(class_a=1, storage=item.size if created else 0)
            self.state.uploaded(item.day, item.key)
            self.state.data["last_upload"] = iso(self.now())
            self.state.save()
            self.queue.done(item)
            LOGGER.debug("%s %s", "Uploaded" if created else "Already in R2:", item.key)
            self._changed()

    async def check_loop(self) -> None:
        """Checks the R2 connection at start and every r2_check_interval
        minutes, so it is known also when nothing is uploaded for hours."""
        while True:
            loop_now = asyncio.get_running_loop().time()
            # Without a usage figure the guard allows nothing: try again soon
            if self.guard.allows(loop_now, utc_month(self.now()), class_b=1):
                try:
                    await self.r2.check()
                except R2Error as err:
                    self._set_r2(not err.unreachable)
                    LOGGER.warning("R2 check: %s", err)
                else:
                    self._set_r2(True)
                self.guard.count(class_b=1)
                await asyncio.sleep(self.cfg.r2_check_interval * 60)
            else:
                await asyncio.sleep(60)

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                self._wake.clear()
                await self._wake.wait()

    # --- Home Assistant -----------------------------------------------------------
    async def ha_loop(self) -> None:
        failures = 0
        while True:
            try:
                await self.ha.connect()
                failures = 0
                config = await self.ha.config()
                try:
                    self.language = str(config.get("language") or "en")
                    self.tz = ZoneInfo(config.get("time_zone") or "UTC")
                except ZoneInfoNotFoundError:
                    LOGGER.warning("Unknown time zone %s; using UTC", config.get("time_zone"))
                    self.tz = ZoneInfo("UTC")
                if not self.state.data["installed"]:
                    self.state.data["installed"] = self.now().astimezone(self.tz).date().isoformat()
                    self.state.save()
                self._ready.set()
                self.ha_connected = True
                self._changed()
                LOGGER.info("Connected to Home Assistant, time zone %s", self.tz.key)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self.follow_log())
                    if self.cfg.alarm_panel:
                        tasks.create_task(self.follow_panel())
                    tasks.create_task(self.ha.wait_closed())
            except* HAError as group:
                LOGGER.warning("Home Assistant: %s; connecting again in %d s", group.exceptions[0],
                               BACKOFF[min(failures, len(BACKOFF) - 1)])
            finally:
                if self.ha_connected:
                    self.ha_connected = False
                    self._changed()
                await self.ha.close()
            await asyncio.sleep(BACKOFF[min(failures, len(BACKOFF) - 1)])
            failures += 1

    async def follow_log(self) -> None:
        # With a fresh index first learn what the bucket holds, so nothing is sent twice
        await self._restored.wait()
        cursor = self.state.data["log_cursor"]
        start = (datetime.fromtimestamp(cursor - 1, UTC) if cursor is not None
                 else self.now() - timedelta(days=FIRST_START_DAYS))
        LOGGER.info("Reading the log of %s from %s", self.cfg.log_entity, iso(start))
        async for event in self.ha.logbook(self.cfg.log_entity, iso(start)):
            newest = self.state.data["log_cursor"]
            for raw in event.get("events") or []:
                entry = a.log_entry(raw, self.cfg.log_entity)
                if entry is None:
                    continue
                key, day, body = a.log_object(entry, self.tz)
                self._enqueue(key, "log", day, body, "application/json")
                newest = entry["when"] if newest is None else max(newest, entry["when"])
            if newest != self.state.data["log_cursor"]:
                self.state.data["log_cursor"] = newest
                self.state.save()

    async def follow_panel(self) -> None:
        panel = self.cfg.alarm_panel
        if self.state.data["panel_cursor"] is None:
            # Snapshots start with the first start, nothing is rebuilt before it
            self.state.data["panel_cursor"] = iso(self.now())
            self.state.save()
        else:
            await self.catch_up_panel()
        # A change between the catch-up and the subscription is rebuilt at the
        # next start: the cursor only moves with changes handled
        async for event in self.ha.panel_changes(panel):
            trigger = (event.get("variables") or {}).get("trigger") or {}
            old, new = trigger.get("from_state") or {}, trigger.get("to_state") or {}
            if not new or new.get("state") == old.get("state"):
                continue
            moment = datetime.fromisoformat(new["last_changed"])
            if new["state"] in self.cfg.snapshot_states:
                await self.snapshot(moment, old.get("state"), new["state"], new.get("attributes") or {},
                                    (new.get("context") or {}).get("user_id"), reconstructed=False)
            self._panel_cursor(moment)

    def _panel_cursor(self, moment: datetime) -> None:
        cursor = self.state.data["panel_cursor"]
        if cursor is None or moment > datetime.fromisoformat(cursor):
            self.state.data["panel_cursor"] = iso(moment)
            self.state.save()

    async def catch_up_panel(self) -> None:
        panel = self.cfg.alarm_panel
        start = self.state.data["panel_cursor"]
        records = (await self.ha.history([panel], start)).get(panel) or []
        previous = records[0].get("s") if records else None
        for record in records[1:]:
            state = record.get("s")
            if state == previous:
                continue
            moment = datetime.fromtimestamp(float(record.get("lc") or record["lu"]), UTC)
            if state in self.cfg.snapshot_states:
                LOGGER.info("Rebuilding the snapshot of %s at %s", state, iso(moment))
                await self.snapshot(moment, previous, state, record.get("a") or {}, None,
                                    reconstructed=True)
            self._panel_cursor(moment)
            previous = state

    async def _entities(self, states: list[dict]) -> list[str]:
        if self.cfg.snapshot_all_entities:
            return sorted(s["entity_id"] for s in states)
        by_id = {s["entity_id"]: s for s in states}
        found: list[str] = []
        todo = [(e, 0) for e in (self.cfg.alarm_panel, *self.cfg.snapshot_entities) if e]
        while todo:
            entity, depth = todo.pop(0)
            if entity in found:
                continue
            found.append(entity)
            members = (by_id.get(entity) or {}).get("attributes", {}).get("entity_id")
            if isinstance(members, list) and depth < GROUP_DEPTH:
                todo += [(m, depth + 1) for m in members if isinstance(m, str)]
        return found

    async def snapshot(self, moment: datetime, old: str | None, new: str, attributes: dict,
                       user_id: str | None, reconstructed: bool) -> None:
        folder, day = a.snapshot_folder(moment, new, self.tz)
        if self._done(day, folder + "meta.json"):
            return
        current = await self.ha.states()
        entities = await self._entities(current)
        if reconstructed:
            history = await self.ha.history(entities, iso(moment), iso(moment + timedelta(milliseconds=1)))
            states = []
            for entity in entities:
                first = (history.get(entity) or [None])[0]
                if first:
                    changed = first.get("lc") or first.get("lu")
                    states.append({"entity_id": entity, "state": first.get("s"),
                                   "attributes": first.get("a") or {},
                                   "last_changed": iso(datetime.fromtimestamp(float(changed), UTC))
                                   if changed else None})
        else:
            wanted = set(entities)
            states = [{"entity_id": s["entity_id"], "state": s.get("state"),
                       "attributes": s.get("attributes") or {}, "last_changed": s.get("last_changed")}
                      for s in current if s["entity_id"] in wanted]
        pictures: dict[str, str | None] = {}
        series: dict[str, list[str]] = {}
        for camera in self.cfg.cameras if new in self.cfg.camera_states else ():
            name = a.camera_file(camera)
            if reconstructed:
                pictures[camera] = None
                continue
            try:
                picture = await self.ha.camera(camera)
            except HAError as err:
                LOGGER.warning("No picture for the snapshot: %s", err)
                pictures[camera] = None
                continue
            self._enqueue(folder + name, "snapshots", day, picture, "image/jpeg")
            pictures[camera] = name
            if self.cfg.camera_pictures > 1:
                series[camera] = [name] + [a.camera_file(camera, n) for n in range(2, self.cfg.camera_pictures + 1)]
        self._enqueue(folder + "zustand.json.gz", "snapshots", day,
                      a.states_body(moment, states, reconstructed), "application/gzip")
        self._enqueue(folder + "meta.json", "snapshots", day,
                      a.meta_body(moment, self.cfg.alarm_panel, old, new, attributes, user_id,
                                  pictures, reconstructed, series), "application/json")
        if series:
            # The rest of the series after the meta file: a restart loses only those pictures
            task = asyncio.create_task(self._take_series(folder, day, series))
            self._series.add(task)
            task.add_done_callback(self._series.discard)
        LOGGER.info("Snapshot %s queued (%d states, %d pictures)", folder, len(states),
                    sum(1 for p in pictures.values() if p))

    async def _take_series(self, folder: str, day: str, series: dict[str, list[str]]) -> None:
        for number in range(1, self.cfg.camera_pictures):
            await asyncio.sleep(self.cfg.camera_interval)
            for camera, names in series.items():
                try:
                    picture = await self.ha.camera(camera)
                except HAError as err:
                    LOGGER.warning("No picture %d of the series: %s", number + 1, err)
                    continue
                self._enqueue(folder + names[number], "snapshots", day, picture, "image/jpeg")

    # --- Index ----------------------------------------------------------------------
    async def restore_index(self) -> None:
        """Fills an empty index from the bucket, e.g. after a new install or
        a lost /data: the web page, the summaries and the log stream then know
        what is archived already. Runs once; with an index it does nothing."""
        data = self.state.data
        if data["uploaded"] or data["summaries"]:
            self._restored.set()
            return
        failures = 0
        while True:
            if not self.guard.allows(asyncio.get_running_loop().time(), utc_month(self.now()), class_a=3):
                await asyncio.sleep(60)
                continue
            try:
                found = {kind: await self.r2.list(prefix) for kind, prefix in a.PREFIX.items()}
            except R2Error as err:
                self._set_r2(not err.unreachable)
                delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
                failures += 1
                LOGGER.warning("Index not read from R2: %s; again in %d s", err, delay)
                await asyncio.sleep(delay)
                continue
            break
        self._set_r2(True)
        # One Class A operation per page of 1000 keys
        self.guard.count(class_a=sum(max(1, -(-len(keys) // 1000)) for keys in found.values()))
        for key in found["log"] + found["snapshots"]:
            if day := a.key_day(key):
                self.state.uploaded(day, key)
        for key in found["summaries"]:
            if parsed := a.summary_part(key):
                day, part = parsed
                keys = data["uploaded"].get(day, [])
                folders = {k.rsplit("/", 1)[0] + "/" for k in keys if k.startswith(a.PREFIX["snapshots"])}
                done = data["summaries"].setdefault(day, {"covered": sorted(set(keys) | folders), "files": 0})
                done["files"] = max(done["files"], part + 1)
        self.state.save()
        if found["log"] or found["snapshots"] or found["summaries"]:
            LOGGER.info("Index read from R2: %d log entries, %d snapshot files, %d summaries of %d days",
                        len(found["log"]), len(found["snapshots"]), len(found["summaries"]),
                        len(set(data["uploaded"]) | set(data["summaries"])))
        self._restored.set()
        self._changed()

    # --- Free tier guard and bucket rules -------------------------------------------
    async def usage_loop(self) -> None:
        if self.cloudflare is None:
            return
        while True:
            now = self.now()
            try:
                usage = await self.cloudflare.usage(now)
            except CloudflareError as err:
                LOGGER.warning("R2 usage not read: %s", err)
            else:
                self.guard.update(usage, asyncio.get_running_loop().time(), utc_month(now))
                self._storage_sample(now, usage)
                LOGGER.debug("R2 usage: %s", usage)
                self._wake.set()
            self._changed()
            await asyncio.sleep(self._next_usage_poll())

    def _storage_sample(self, now: datetime, usage) -> None:
        """Keeps the highest storage per UTC day for the growth estimate (60 days)."""
        days = self.state.data["storage_days"]
        day = now.astimezone(UTC).date().isoformat()
        old = days.get(day) or [0, 0]
        new = [max(old[0], usage.storage_bytes), max(old[1], usage.bucket_storage_bytes)]
        if new != old:
            days[day] = new
            for stale in sorted(days)[:-60]:
                del days[stale]
            self.state.save()

    def _next_usage_poll(self) -> float:
        """Every 15 minutes, and right after the month changes: last month's
        figure no longer counts."""
        now = self.now()
        next_month = (now.replace(day=28) + timedelta(days=4)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, min(USAGE_INTERVAL, (next_month - now).total_seconds() + 5))

    async def token_loop(self) -> None:
        if self.cloudflare is None:
            return
        while True:
            try:
                expiry = await self.cloudflare.token_expiry()
            except CloudflareError as err:
                LOGGER.warning("api_token not verified: %s", err)
                await asyncio.sleep(3600)
                continue
            if not self.token_checked or expiry != self.token_expiry:
                self.token_checked, self.token_expiry = True, expiry
                self._changed()
            if self.token_expiring():
                LOGGER.warning("api_token expires on %s; renew it and set the new one", iso(expiry))
            await asyncio.sleep(TOKEN_INTERVAL)

    async def rules_loop(self) -> None:
        if self.cloudflare is None:
            if any(v is not None for v in (*self.cfg.retention.values(), *self.cfg.lock.values())):
                LOGGER.warning("Without api_token the bucket's lifecycle and lock rules are not checked")
            return
        while True:
            try:
                lifecycle = await self.cloudflare.lifecycle_rules()
                locks = await self.cloudflare.lock_rules()
            except CloudflareError as err:
                LOGGER.warning("Bucket rules not read: %s", err)
                problems = None
            else:
                problems = a.rule_problems(lifecycle, locks, self.cfg.retention, self.cfg.lock)
                for problem in problems:
                    LOGGER.warning("Bucket rules: %s", problem)
                if problems:
                    LOGGER.warning("To match the options: %s",
                                   "; ".join(a.rule_commands(self.cfg.bucket, self.cfg.retention,
                                                             self.cfg.lock, self.cfg.jurisdiction)))
            if problems != self.rule_problems:
                self.rule_problems = problems
                self._changed()
            await asyncio.sleep(RULES_INTERVAL if problems is not None else 900)

    # --- Daily summary ------------------------------------------------------------------
    def _summary_days(self, today: date) -> list[str]:
        """Days before today that need a summary or a supplement."""
        days = set()
        installed = self.state.data["installed"]
        if installed:
            day = date.fromisoformat(installed)
            while day < today:
                days.add(day.isoformat())
                day += timedelta(days=1)
        days |= {d for d in self.state.data["uploaded"] if date.fromisoformat(d) < today}
        waiting = self.queue.days()
        out = []
        for day in sorted(days):
            done = self.state.data["summaries"].get(day)
            uploaded = set(self.state.data["uploaded"].get(day, []))
            if day in waiting:
                continue
            if done is None or not uploaded <= set(done["covered"]):
                out.append(day)
        return out

    async def summary_loop(self) -> None:
        await self._ready.wait()
        await self._restored.wait()
        while True:
            local = self.now().astimezone(self.tz)
            today = local.date()
            for day in self._summary_days(today):
                after = datetime.combine(date.fromisoformat(day) + timedelta(days=1), SUMMARY_AT, self.tz)
                if local < after:
                    continue
                try:
                    await self.summarize(day)
                except R2Error as err:
                    self._set_r2(not err.unreachable)
                    LOGGER.warning("Summary of %s not written: %s", day, err)
                    break
            await asyncio.sleep(60)

    async def summarize(self, day: str) -> bool:
        """Writes the summary of a day, or a supplement with what came later.
        False when the guard held it back."""
        month = utc_month(self.now())
        loop = asyncio.get_running_loop()
        if not self.guard.allows(loop.time(), month, class_a=2):
            return False
        folder = a.day_folder(day)
        log_keys = await self.r2.list(a.PREFIX["log"] + folder)
        snapshot_keys = await self.r2.list(a.PREFIX["snapshots"] + folder)
        self.guard.count(class_a=2)
        self._set_r2(True)
        done = self.state.data["summaries"].get(day) or {"covered": [], "files": 0}
        covered = set(done["covered"])
        new_logs = [k for k in log_keys if k not in covered]
        new_folders = sorted({k.rsplit("/", 1)[0] + "/" for k in snapshot_keys} - covered)
        all_covered = sorted(covered | set(log_keys) | set(snapshot_keys) | set(new_folders)
                             | set(self.state.data["uploaded"].get(day, [])))
        if done["files"] and not new_logs and not new_folders:
            done["covered"] = all_covered
            self.state.data["summaries"][day] = done
            self.state.save()
            return True
        if not self.guard.allows(loop.time(), month, class_b=len(new_logs)):
            return False
        entries = [(key, await self.r2.get(key)) for key in new_logs]
        self.guard.count(class_b=len(new_logs))
        part = done["files"]
        while True:
            body = a.summary_text(day, entries, new_folders, self.tz, part, self.now())
            if not self.guard.allows(loop.time(), month, class_a=1, storage=len(body)):
                return False
            created = await self.r2.put_once(a.summary_key(day, part), body, "text/plain; charset=utf-8")
            self.guard.count(class_a=1, storage=len(body) if created else 0)
            if created:
                break
            part += 1      # the file is there already, e.g. after /data was lost
        self.state.data["summaries"][day] = {"covered": all_covered, "files": part + 1}
        self.state.data["last_summary"] = iso(self.now())
        self.state.save()
        LOGGER.info("Summary %s written (%d entries)", a.summary_key(day, part), len(entries))
        self._changed()
        return True
