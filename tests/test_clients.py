"""The Home Assistant websocket client and the Cloudflare API client against
small fakes of both."""

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web

from r2_archive.cloudflare import Cloudflare, CloudflareError, Usage
from r2_archive.ha import HA, HAError


class FakeCore:
    def __init__(self):
        self.token = "supervisor-token"
        self.sockets = []

        self.down = False

    def app(self):
        app = web.Application()
        app.router.add_get("/websocket", self.websocket)
        app.router.add_get("/api/camera_proxy/{entity}", self.camera)
        return app

    async def camera(self, request):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        if request.match_info["entity"] == "camera.front":
            return web.Response(body=b"jpeg", content_type="image/jpeg")
        return web.Response(status=500)

    async def websocket(self, request):
        if self.down:
            # The Supervisor's proxy while Core restarts
            return web.Response(status=502)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            data = msg.json()
            kind, msg_id = data["type"], data["id"]
            if kind == "get_config":
                await ws.send_json({"id": msg_id, "type": "result", "success": True,
                                    "result": {"time_zone": "Europe/Berlin"}})
            elif kind == "logbook/event_stream":
                assert data["entity_ids"] == ["input_boolean.alarm_log"]
                await ws.send_json({"id": msg_id, "type": "result", "success": True, "result": None})
                # Historical and live events can come batched in one frame
                await ws.send_json([
                    {"id": msg_id, "type": "event", "event": {"events": [{"message": "alt", "when": 1.0}]}},
                    {"id": msg_id, "type": "event", "event": {"events": [{"message": "neu", "when": 2.0}]}},
                ])
            elif kind == "history/history_during_period":
                await ws.send_json({"id": msg_id, "type": "result", "success": True,
                                    "result": {e: [{"s": "on", "lu": 1.0}] for e in data["entity_ids"]}})
            else:
                await ws.send_json({"id": msg_id, "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": "Unknown command."}})
        return ws


@pytest.fixture
async def core(aiohttp_server):
    fake = FakeCore()
    server = await aiohttp_server(fake.app())
    async with aiohttp.ClientSession() as http:
        yield fake, HA(http, fake.token, base=f"http://{server.host}:{server.port}"), http, server


async def test_websocket(core):
    fake, ha, _, _ = core
    await ha.connect()
    assert (await ha.config())["time_zone"] == "Europe/Berlin"
    history = await ha.history(["a.b"], "2026-10-31T00:00:00+00:00")
    assert history == {"a.b": [{"s": "on", "lu": 1.0}]}
    events = []
    async for event in ha.logbook("input_boolean.alarm_log", "2026-10-01T00:00:00+00:00"):
        events.append(event["events"][0]["message"])
        if len(events) == 2:
            break
    assert events == ["alt", "neu"]
    with pytest.raises(HAError, match="Unknown command"):
        await ha.call({"type": "nope"})
    assert await ha.camera("camera.front") == b"jpeg"
    with pytest.raises(HAError, match="HTTP 500"):
        await ha.camera("camera.kaputt")
    await ha.close()


async def test_websocket_closed_ends_waits(core):
    fake, ha, _, _ = core
    await ha.connect()
    stream = ha.logbook("input_boolean.alarm_log", "2026-10-01T00:00:00+00:00")
    await anext(stream)
    await fake.sockets[0].close()
    with pytest.raises(HAError):
        async with asyncio.timeout(2):
            async for _ in stream:
                pass
    with pytest.raises(HAError):
        await ha.wait_closed()
    await ha.close()


async def test_restart_is_named(core):
    """Home Assistant closing the line and the Supervisor's 502 while Core is
    away both say that Home Assistant is gone, not that the app failed."""
    fake, ha, _, _ = core
    await ha.connect()
    stream = ha.logbook("input_boolean.alarm_log", "2026-10-01T00:00:00+00:00")
    await anext(stream)
    await fake.sockets[0].close()
    with pytest.raises(HAError, match="closed the connection") as closed:
        async with asyncio.timeout(2):
            async for _ in stream:
                pass
    assert closed.value.down
    with pytest.raises(HAError, match="closed the connection"):
        await ha.wait_closed()
    await ha.close()
    fake.down = True
    with pytest.raises(HAError, match="Supervisor answers 502") as refused:
        await ha.connect()
    assert refused.value.down


async def test_websocket_refused(core):
    _, _, http, server = core
    ha = HA(http, "wrong", base=f"http://{server.host}:{server.port}")
    with pytest.raises(HAError, match="refused"):
        await ha.connect()


class FakeApi:
    def __init__(self):
        self.status = 200
        self.queries = []
        self.expires_on = "2027-01-31T23:59:59Z"

    def app(self):
        app = web.Application()
        app.router.add_post("/graphql", self.graphql)
        app.router.add_get("/accounts/{account}/r2/buckets/{bucket}/{what}", self.rules)
        app.router.add_get("/accounts/{account}/tokens/verify", self.account_token)
        app.router.add_get("/user/tokens/verify", self.user_token)
        return app

    async def account_token(self, request):
        # A user token is no account token
        return web.json_response({"success": False, "errors": [{"code": 1000}]}, status=401)

    async def user_token(self, request):
        return web.json_response({"success": True, "result": {"id": "t", "status": "active",
                                                               "expires_on": self.expires_on}})

    async def graphql(self, request):
        if self.status != 200:
            return web.Response(status=self.status)
        body = await request.json()
        self.queries.append((request.headers, body["variables"]))
        return web.json_response({"data": {"viewer": {"accounts": [{
            "operations": [{"sum": {"requests": 3}, "dimensions": {"actionType": "PutObject"}}],
            "storage": [{"max": {"payloadSize": 9, "metadataSize": 1}, "dimensions": {"bucketName": "x"}}],
        }]}}, "errors": None})

    async def rules(self, request):
        what = request.match_info["what"]
        return web.json_response({"success": True, "errors": [], "result": {"rules": [{"id": what}]}})


async def test_cloudflare(aiohttp_server):
    fake = FakeApi()
    server = await aiohttp_server(fake.app())
    async with aiohttp.ClientSession() as http:
        api = Cloudflare(http, "tok", "acc", "bucket", "eu", api=f"http://{server.host}:{server.port}")
        usage = await api.usage(datetime(2026, 10, 31, 8, 0, tzinfo=UTC))
        assert usage == Usage(3, 0, 10)
        headers, variables = fake.queries[0]
        assert headers["Authorization"] == "Bearer tok" and headers["cf-r2-jurisdiction"] == "eu"
        assert variables["monthStart"] == "2026-10-01T00:00:00+00:00"
        assert variables["dayStart"] == "2026-10-30T08:00:00+00:00"
        assert await api.lifecycle_rules() == [{"id": "lifecycle"}]
        assert await api.lock_rules() == [{"id": "lock"}]
        assert await api.token_expiry() == datetime(2027, 1, 31, 23, 59, 59, tzinfo=UTC)
        fake.expires_on = None
        assert await api.token_expiry() is None
        fake.status = 403
        with pytest.raises(CloudflareError, match="refused"):
            await api.usage(datetime(2026, 10, 31, tzinfo=UTC))


async def test_stop_is_no_lost_connection(core):
    """Cancelling the wait (the app stops) must not turn into 'websocket closed'."""
    _, ha, _, _ = core
    await ha.connect()
    waiter = asyncio.create_task(ha.wait_closed())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await ha.close()
