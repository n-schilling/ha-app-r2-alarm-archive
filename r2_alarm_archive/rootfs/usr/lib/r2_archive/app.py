"""The archive: follows the alarm log, the alarm panel and snapshot requests
in Home Assistant, queues what belongs in the bucket, uploads it past the
free tier guard and writes the daily summaries.

Everything that must reach R2 goes through the queue in /data first, so an
outage of R2 or of the network loses nothing. The log stream resumes where
the last queued entry was; panel changes the app missed are rebuilt from
the recorder. Snapshot requests fired while the app is not connected are
not rebuilt: Home Assistant keeps no record the app could read them from.
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
from .store import Item, Queue, State

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
# A restart of Home Assistant is quiet up to here, then it is worth a warning
HA_AWAY_WARN = timedelta(minutes=15)
SUMMARY_GRACE = timedelta(hours=1)
# api_token: checked daily, warned about this long before it expires
TOKEN_INTERVAL = 24 * 3600
TOKEN_WARN = timedelta(days=14)
# The state file is written at the latest this long after an upload, or when the queue is empty
SAVE_EVERY = 5.0

# Why archiving does not work, in English for the MQTT attribute; the web page
# translates them by their code
PROBLEMS = {
    "ha": "Home Assistant not connected",
    "r2": "R2 not reachable",
    "r2_check": "R2 check failed: {error}",
    "upload": "last upload failed: {error}",
    "quarantine": "{count} objects in quarantine",
    "waiting": "{count} objects waiting, the oldest for {minutes} min",
    "limit": "free tier limit reached",
    "no_figure": "no R2 usage figure for the free tier guard",
    "summary": "summary of {day} missing",
}


class Problem(str):
    """A problem as its English text, with its code and values for translations."""

    code: str
    params: dict[str, object]

    def __new__(cls, code: str, **params: object) -> Problem:
        self = super().__new__(cls, PROBLEMS[code].format(**params))
        self.code, self.params = code, params
        return self


def duration(span: timedelta) -> str:
    minutes, seconds = divmod(round(span.total_seconds()), 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def pages(keys: list[str]) -> int:
    """List requests (Class A operations) for this many keys, 1000 per page."""
    return max(1, -(-len(keys) // 1000))


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
        self.check_error: str | None = None       # R2 answered the last check with this error
        self.rules_logged: str | None = None
        self.token_checked = False           # api_token verified at least once
        self.token_expiry: datetime | None = None   # None: it never expires
        self.changed = asyncio.Event()       # a status value changed
        self._wake = asyncio.Event()         # the queue has something new
        self._ready = asyncio.Event()        # the time zone is known
        self._restored = asyncio.Event()     # the index is there, or was read from R2
        self._series: set[asyncio.Task] = set()     # picture series still being taken

    # --- Status ---------------------------------------------------------------
    def health(self) -> list[Problem]:
        """Why archiving does not work right now; empty while it does: Home
        Assistant connected, R2 takes the key, uploads succeed, nothing waits
        too long, the guard has a figure below the limit, no summary is overdue."""
        reasons = []
        if not self.ha_connected:
            reasons.append(Problem("ha"))
        if self.r2_connected is False:
            reasons.append(Problem("r2"))
        if self.check_error:
            reasons.append(Problem("r2_check", error=self.check_error))
        if self.upload_error:
            reasons.append(Problem("upload", error=self.upload_error))
        if set_aside := {q.name.rsplit(".", 1)[0] for q in self.queue.quarantined()}:
            reasons.append(Problem("quarantine", count=len(set_aside)))
        oldest = self.queue.oldest()
        if oldest is not None and clock_time.time() - oldest > STUCK_AFTER:
            reasons.append(Problem("waiting", count=len(self.queue),
                                   minutes=int((clock_time.time() - oldest) // 60)))
        limit = self.guard.limit_reached(asyncio.get_running_loop().time(), utc_month(self.now()))
        if limit:
            reasons.append(Problem("limit"))
        elif limit is None:
            reasons.append(Problem("no_figure"))
        if self.tz is not None:
            local = self.now().astimezone(self.tz)
            for day in self._summary_days(local.date()):
                due = datetime.combine(date.fromisoformat(day) + timedelta(days=1), SUMMARY_AT, self.tz)
                if local > due + SUMMARY_GRACE:
                    reasons.append(Problem("summary", day=day))
                    break
        return reasons

    def status(self) -> dict[str, object]:
        loop_now = asyncio.get_running_loop().time()
        month = utc_month(self.now())
        percent = self.guard.percent()
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

    def _set_check_error(self, error: str | None) -> None:
        if self.check_error != error:
            self.check_error = error
            self._changed()

    def _set_r2(self, connected: bool) -> None:
        if self.r2_connected != connected:
            self.r2_connected = connected
            self._changed()

    # --- Queue ------------------------------------------------------------------
    def _done(self, day: str, key: str) -> bool:
        return self.state.has(day, key) or key in self.queue

    def _enqueue(self, key: str, kind: str, day: str, body: bytes, content_type: str) -> None:
        if not self._done(day, key) and self.queue.add(key, kind, day, body, content_type):
            self._wake.set()
            self._changed()

    async def upload_loop(self) -> None:
        """Uploads the queue in order. The state file is written for a batch
        of uploads, and only then are they taken off the queue: a crash in
        between uploads them again, which R2 answers with 'there already'."""
        failures = 0
        uploaded: list[Item] = []      # in R2, not yet in the saved state
        since = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                if uploaded and loop.time() - since >= SAVE_EVERY:
                    self._save_uploaded(uploaded)
                waiting = {item.seq for item in uploaded}
                items = [item for item in self.queue.items() if item.seq not in waiting]
                if not items:
                    self._save_uploaded(uploaded)
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                item = items[0]
                if not uploaded:
                    since = loop.time()
                result = await self._upload(item, failures)
                if result is True:
                    failures = 0
                    uploaded.append(item)
                elif result is False:
                    failures += 1
        finally:
            self._save_uploaded(uploaded)

    def _save_uploaded(self, uploaded: list[Item]) -> None:
        if not uploaded:
            return
        self.state.save()
        for item in uploaded:
            self.queue.done(item)
        uploaded.clear()
        self._changed()

    async def _upload(self, item: Item, failures: int) -> bool | None:
        """One upload: True when R2 has the object, False when the upload
        failed (after the backoff delay), None when it waits for the guard or
        the item went to the quarantine."""
        loop_now = asyncio.get_running_loop().time()
        if not self.guard.allows(loop_now, utc_month(self.now()), class_a=1, storage=item.size):
            self._changed()
            await self._sleep_or_wake(60)
            return None
        try:
            body = self.queue.body(item)
        except OSError as err:
            # The body file is gone or broken: set it aside instead of blocking the queue
            self.queue.set_aside(item, err)
            self._changed()
            return None
        try:
            created = await self.r2.put_once(item.key, body, item.content_type)
        except R2Error as err:
            self._set_r2(not err.unreachable)
            self.upload_error = str(err)
            self._changed()
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            level = logging.WARNING if err.unreachable or failures == 0 else logging.ERROR
            if isinstance(err, R2AuthError):
                level = logging.ERROR
            LOGGER.log(level, "Upload of %s failed: %s; again in %d s", item.key, err, delay)
            await asyncio.sleep(delay)
            return False
        self._set_r2(True)
        self.upload_error = None
        self.guard.count(class_a=1, storage=item.size if created else 0)
        self.state.uploaded(item.day, item.key)
        self.state.data["last_upload"] = iso(self.now())
        LOGGER.debug("%s %s", "Uploaded" if created else "Already in R2:", item.key)
        self._changed()
        return True

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
                    # R2 answered, but refuses: e.g. a revoked key or a deleted bucket
                    self._set_check_error(None if err.unreachable else str(err))
                    LOGGER.warning("R2 check: %s", err)
                else:
                    self._set_r2(True)
                    self._set_check_error(None)
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
        away: datetime | None = None   # since when Home Assistant is gone
        warned = False
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
                if away is None:
                    LOGGER.info("Connected to Home Assistant, time zone %s", self.tz.key)
                else:
                    LOGGER.info("Connected to Home Assistant again after %s, time zone %s",
                                duration(self.now() - away), self.tz.key)
                away, warned = None, False
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self.follow_log())
                    if self.cfg.alarm_panel:
                        tasks.create_task(self.follow_panel())
                    tasks.create_task(self.follow_requests())
                    tasks.create_task(self.ha.wait_closed())
            except* HAError as group:
                err = group.exceptions[0]
                delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
                if not err.down:
                    LOGGER.warning("Home Assistant: %s; connecting again in %d s", err, delay)
                elif away is None:
                    LOGGER.info("Home Assistant %s; connecting again when it is back", err)
                elif not warned and self.now() - away >= HA_AWAY_WARN:
                    LOGGER.warning("Home Assistant still away after %s: %s",
                                   duration(self.now() - away), err)
                    warned = True
                if away is None:
                    away = self.now()
            except* Exception as group:
                # A fault of this app while following Home Assistant: log it and
                # connect again, archiving and the upload go on meanwhile
                LOGGER.error("Following Home Assistant failed; connecting again",
                             exc_info=group.exceptions[0])
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
        # From just after the cursor: the state at that moment is the change the
        # cursor points to, which is handled. Asked from the cursor itself, Home
        # Assistant may answer with the state before that change instead, and the
        # row of the change, or the row it writes for the panel's unchanged state
        # when it starts, would pass as a change
        start = datetime.fromisoformat(self.state.data["panel_cursor"]) + timedelta(microseconds=1)
        records = (await self.ha.history([panel], iso(start))).get(panel) or []
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

    async def follow_requests(self) -> None:
        """A snapshot with pictures of all cameras for each event
        r2_alarm_archive_snapshot, e.g. from an automation while the panel is
        disarmed. Optional event data: `reason`."""
        async for event in self.ha.snapshot_requests():
            data = event.get("data") or {}
            try:
                moment = datetime.fromisoformat(event["time_fired"])
            except (KeyError, TypeError, ValueError):
                moment = self.now()
            await self.snapshot(moment, None, a.REQUEST, {}, (event.get("context") or {}).get("user_id"),
                                reconstructed=False, reason=a.request_reason(
                                    data.get("reason") if isinstance(data, dict) else None))

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
                       user_id: str | None, reconstructed: bool, reason: str | None = None) -> None:
        """A snapshot of the panel's change to `new`, or with `new` = request
        one an automation asked for: then `old` and `attributes` come from the
        panel as it is, and every camera gives pictures."""
        requested = new == a.REQUEST
        folder, day = a.snapshot_folder(moment, new, self.tz)
        if requested:
            # Every request is kept, also two at the same moment: the later one
            # gets the next free microsecond in its folder name
            slot = moment
            while self._done(day, folder + "meta.json"):
                slot += timedelta(microseconds=1)
                folder, day = a.snapshot_folder(slot, new, self.tz)
        elif self._done(day, folder + "meta.json"):
            return
        cameras = list(self.cfg.cameras if requested or new in self.cfg.camera_states else ())
        # The pictures first and all at once, so they are closest to the moment;
        # a rebuilt snapshot has none
        taking = asyncio.ensure_future(self._pictures([] if reconstructed else cameras))
        try:
            current = await self.ha.states()
        except BaseException:
            taking.cancel()
            raise
        entities = await self._entities(current)
        if requested:
            panel = next((s for s in current if s["entity_id"] == self.cfg.alarm_panel), None) \
                if self.cfg.alarm_panel else None
            old = panel.get("state") if panel else None
            attributes = (panel or {}).get("attributes") or {}
        attributes = a.without_secrets(attributes)
        if reconstructed:
            history = await self.ha.history(entities, iso(moment), iso(moment + timedelta(milliseconds=1)))
            states = []
            for entity in entities:
                first = (history.get(entity) or [None])[0]
                if first:
                    changed = first.get("lc") or first.get("lu")
                    states.append({"entity_id": entity, "state": first.get("s"),
                                   "attributes": a.without_secrets(first.get("a") or {}),
                                   "last_changed": iso(datetime.fromtimestamp(float(changed), UTC))
                                   if changed else None})
        else:
            wanted = set(entities)
            states = [{"entity_id": s["entity_id"], "state": s.get("state"),
                       "attributes": a.without_secrets(s.get("attributes") or {}),
                       "last_changed": s.get("last_changed")}
                      for s in current if s["entity_id"] in wanted]
        pictures: dict[str, str | None] = {}
        series: dict[str, list[str]] = {}
        shots = await taking
        for camera in cameras:
            name = a.camera_file(camera)
            picture = shots.get(camera)
            if not isinstance(picture, bytes):
                if picture is not None:
                    LOGGER.warning("No picture for the snapshot: %s", picture)
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
                                  pictures, reconstructed, series, reason), "application/json")
        if series:
            # The rest of the series after the meta file: a restart loses only those pictures
            task = asyncio.create_task(self._take_series(folder, day, series))
            self._series.add(task)
            task.add_done_callback(self._series_done)
        LOGGER.info("Snapshot %s queued (%d states, %d pictures)%s", folder, len(states),
                    sum(1 for p in pictures.values() if p), f": {reason}" if reason else "")

    async def _pictures(self, cameras: list[str]) -> dict[str, bytes | HAError]:
        """One picture of each camera, taken at the same time."""
        results = await asyncio.gather(*(self.ha.camera(c) for c in cameras), return_exceptions=True)
        out: dict[str, bytes | HAError] = {}
        for camera, result in zip(cameras, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, HAError):
                raise result
            out[camera] = result
        return out

    async def _take_series(self, folder: str, day: str, series: dict[str, list[str]]) -> None:
        """The further pictures of a series, every camera_interval seconds from
        the first, however long a picture takes."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        for number in range(1, self.cfg.camera_pictures):
            await asyncio.sleep(max(0.0, start + number * self.cfg.camera_interval - loop.time()))
            shots = await self._pictures(list(series))
            for camera, names in series.items():
                picture = shots[camera]
                if isinstance(picture, HAError):
                    LOGGER.warning("No picture %d of the series: %s", number + 1, picture)
                    continue
                self._enqueue(folder + names[number], "snapshots", day, picture, "image/jpeg")

    def _series_done(self, task: asyncio.Task) -> None:
        self._series.discard(task)
        if not task.cancelled() and (err := task.exception()) is not None:
            LOGGER.error("Picture series failed", exc_info=err)

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
        self.guard.count(class_a=sum(pages(keys) for keys in found.values()))
        # Only keys as the app writes them: anything else in the bucket (other
        # tools, or somebody with the key) never reaches the index and the page
        foreign = 0
        for key in found["log"] + found["snapshots"]:
            if a.valid_key(key) and (day := a.key_day(key)):
                self.state.uploaded(day, key)
            else:
                foreign += 1
        for key in found["summaries"]:
            if parsed := a.summary_part(key):
                day, part = parsed
                keys = data["uploaded"].get(day, [])
                folders = {k.rsplit("/", 1)[0] + "/" for k in keys if k.startswith(a.PREFIX["snapshots"])}
                done = data["summaries"].get(day) or {"covered": sorted(set(keys) | folders), "files": 0}
                self.state.set_summary(day, done["covered"], max(done["files"], part + 1))
            else:
                foreign += 1
        if foreign:
            LOGGER.warning("Index: %d keys in the bucket are not the app's and were left out", foreign)
        self.state.save()
        if found["log"] or found["snapshots"] or found["summaries"]:
            LOGGER.info("Index read from R2: %d log entries, %d snapshot files, %d summaries of %d days",
                        len(found["log"]), len(found["snapshots"]), len(found["summaries"]),
                        len(set(data["uploaded"]) | set(data["summaries"])))
        self._restored.set()
        self._changed()

    # --- Free tier guard and the bucket's rules -------------------------------------------
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
        """Logs the bucket's lifecycle and lock rules once a day, at INFO and only when they
        change: they belong to the Cloudflare account, not to the app, so they are no problem
        of the app's whatever they say."""
        if self.cloudflare is None:
            return
        while True:
            try:
                text = a.describe_rules(await self.cloudflare.lifecycle_rules(),
                                        await self.cloudflare.lock_rules())
            except CloudflareError as err:
                text = f"not read ({err})"
            if text != self.rules_logged:
                LOGGER.info("Bucket rules (set in Cloudflare, not by the app): %s", text)
                self.rules_logged = text
            await asyncio.sleep(RULES_INTERVAL)

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
        return [day for day in sorted(days) if day not in waiting and not self.state.summary_complete(day)]

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
        # One Class A operation per page of 1000 keys
        self.guard.count(class_a=pages(log_keys) + pages(snapshot_keys))
        log_keys = [k for k in log_keys if a.valid_key(k)]
        snapshot_keys = [k for k in snapshot_keys if a.valid_key(k)]
        self._set_r2(True)
        done = self.state.data["summaries"].get(day) or {"covered": [], "files": 0}
        covered = set(done["covered"])
        new_logs = [k for k in log_keys if k not in covered]
        new_folders = sorted({k.rsplit("/", 1)[0] + "/" for k in snapshot_keys} - covered)
        all_covered = sorted(covered | set(log_keys) | set(snapshot_keys) | set(new_folders)
                             | set(self.state.data["uploaded"].get(day, [])))
        if done["files"] and not new_logs and not new_folders:
            self.state.set_summary(day, all_covered, done["files"])
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
        self.state.set_summary(day, all_covered, part + 1)
        self.state.data["last_summary"] = iso(self.now())
        self.state.save()
        LOGGER.info("Summary %s written (%d entries)", a.summary_key(day, part), len(entries))
        self._changed()
        return True
