"""Home Assistant through the Supervisor's proxy: the websocket for the
logbook stream, panel changes, snapshot requests, states and history, and
the REST API for camera pictures."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

CORE = "http://supervisor/core"
# The event an automation fires to ask for a snapshot
SNAPSHOT_EVENT = "r2_alarm_archive_snapshot"


# What the Supervisor's proxy answers while Core is stopped or starting
CORE_DOWN = (502, 503, 504)


class HAError(Exception):
    """Home Assistant could not be reached or refused a request. `down` is set
    when Home Assistant itself is away (it closed the connection, or the
    Supervisor cannot reach it): a restart, not a fault of this app."""

    def __init__(self, message: str, down: bool = False) -> None:
        super().__init__(message)
        self.down = down


class HA:
    def __init__(self, http: aiohttp.ClientSession, token: str, base: str = CORE) -> None:
        self._http = http
        self._token = token
        self._base = base
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._results: dict[int, asyncio.Future] = {}
        self._streams: dict[int, asyncio.Queue] = {}
        self._reader: asyncio.Task | None = None
        self._lost: HAError | None = None

    # --- Connection -----------------------------------------------------------
    async def connect(self) -> None:
        self._lost = None
        try:
            self._ws = await self._http.ws_connect(f"{self._base}/websocket".replace("http", "ws", 1),
                                                   heartbeat=30, max_msg_size=0)
            hello = await self._ws.receive_json()
            if hello.get("type") != "auth_required":
                raise HAError(f"unexpected greeting {hello}")
            await self._ws.send_json({"type": "auth", "access_token": self._token})
            answer = await self._ws.receive_json()
        except aiohttp.WSServerHandshakeError as err:
            if err.status in CORE_DOWN:
                raise HAError(f"is not running (Supervisor answers {err.status})", down=True) from err
            raise HAError(f"websocket: {err}") from err
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as err:
            raise HAError(f"websocket: {err or type(err).__name__}") from err
        if answer.get("type") != "auth_ok":
            raise HAError(f"websocket login refused: {answer.get('message', answer)}")
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._ws:
            await self._ws.close()

    async def wait_closed(self) -> None:
        """Returns when the websocket is gone; raises HAError then. A stop of
        the app is no lost connection: its cancellation passes through."""
        if self._reader:
            try:
                await self._reader
            except asyncio.CancelledError:
                raise
            except Exception as err:
                raise self._lost or HAError(f"websocket: {err or type(err).__name__}") from err
        raise self._lost or HAError("websocket closed")

    def _why_closed(self) -> HAError:
        """A closing handshake comes from Home Assistant going away (the
        Supervisor's proxy passes it on); without one (1006) or after an error
        the line itself broke, e.g. no answer to the heartbeat."""
        error = self._ws.exception()
        if error is not None or self._ws.close_code in (None, aiohttp.WSCloseCode.ABNORMAL_CLOSURE):
            return HAError(f"connection lost: {error or 'no answer from Home Assistant'}")
        return HAError("closed the connection (restart or stop)", down=True)

    async def _read(self) -> None:
        lost = HAError("websocket closed")
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = msg.json()
                for item in data if isinstance(data, list) else [data]:
                    self._dispatch(item)
            lost = self._why_closed()
        except Exception as err:
            lost = HAError(f"connection lost: {err or type(err).__name__}")
            raise
        finally:
            # Everyone waiting learns the same reason
            self._lost = lost
            for future in self._results.values():
                if not future.done():
                    future.set_exception(lost)
            self._results.clear()
            for queue in self._streams.values():
                queue.put_nowait(None)

    def _dispatch(self, item: dict) -> None:
        msg_id = item.get("id")
        if item.get("type") == "result" and msg_id in self._results:
            future = self._results.pop(msg_id)
            if future.done():
                return
            if item.get("success"):
                future.set_result(item.get("result"))
            else:
                future.set_exception(HAError(f"{(item.get('error') or {}).get('message', item)}"))
        elif item.get("type") == "event" and msg_id in self._streams:
            self._streams[msg_id].put_nowait(item.get("event"))

    async def call(self, message: dict[str, Any], timeout: float = 60) -> Any:
        if self._ws is None or self._ws.closed:
            raise HAError("websocket not connected")
        msg_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._results[msg_id] = future
        await self._ws.send_json({"id": msg_id, **message})
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as err:
            self._results.pop(msg_id, None)
            raise HAError(f"{message.get('type')}: no answer in {timeout:.0f} s") from err

    async def subscribe(self, message: dict[str, Any]) -> AsyncIterator[dict]:
        """Events of a subscription until the websocket closes (then HAError)."""
        if self._ws is None or self._ws.closed:
            raise HAError("websocket not connected")
        msg_id = next(self._ids)
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[msg_id] = queue
        future = asyncio.get_running_loop().create_future()
        self._results[msg_id] = future
        await self._ws.send_json({"id": msg_id, **message})
        await future
        try:
            while True:
                event = await queue.get()
                if event is None:
                    raise self._lost or HAError("websocket closed")
                yield event
        finally:
            self._streams.pop(msg_id, None)

    # --- Requests ---------------------------------------------------------------
    async def config(self) -> dict:
        return await self.call({"type": "get_config"})

    async def states(self) -> list[dict]:
        return await self.call({"type": "get_states"})

    def logbook(self, entity_id: str, start: str) -> AsyncIterator[dict]:
        """Log entries from `start` (ISO time), first those in the recorder,
        then live ones."""
        return self.subscribe({"type": "logbook/event_stream", "start_time": start,
                               "entity_ids": [entity_id]})

    def panel_changes(self, entity_id: str) -> AsyncIterator[dict]:
        return self.subscribe({"type": "subscribe_trigger",
                               "trigger": {"platform": "state", "entity_id": entity_id}})

    def snapshot_requests(self) -> AsyncIterator[dict]:
        """Events of type r2_alarm_archive_snapshot as they are fired; those
        fired while the websocket is closed are gone."""
        return self.subscribe({"type": "subscribe_events", "event_type": SNAPSHOT_EVENT})

    async def history(self, entity_ids: list[str], start: str, end: str | None = None,
                      attributes: bool = True) -> dict[str, list[dict]]:
        """States from `start` on, the first one per entity being its state at
        `start`; compressed keys s (state), a (attributes), lc/lu (times)."""
        message = {"type": "history/history_during_period", "start_time": start,
                   "entity_ids": entity_ids, "include_start_time_state": True,
                   "significant_changes_only": False, "minimal_response": False,
                   "no_attributes": not attributes}
        if end:
            message["end_time"] = end
        return await self.call(message, timeout=120)

    async def camera(self, entity_id: str) -> bytes:
        try:
            async with self._http.get(f"{self._base}/api/camera_proxy/{entity_id}",
                                      headers={"Authorization": f"Bearer {self._token}"},
                                      timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    raise HAError(f"{entity_id}: HTTP {resp.status}")
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HAError(f"{entity_id}: {err or type(err).__name__}") from err
