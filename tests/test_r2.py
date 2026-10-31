"""Signature V4 against AWS's published examples, and the client against a
small fake of the S3 API."""

import hashlib
from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web

from r2_archive.r2 import EMPTY_SHA256, R2, R2AuthError, R2Error, endpoint, query_string, sign

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
ACCESS = "AKIAIOSFODNN7EXAMPLE"
WHEN = datetime(2013, 5, 24, tzinfo=UTC)


def signature(headers):
    return headers["authorization"].rsplit("Signature=", 1)[1]


def test_get_object_example():
    """docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html, GET Object"""
    headers = sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"},
                   EMPTY_SHA256, ACCESS, SECRET, WHEN, region="us-east-1")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["authorization"]
    assert signature(headers) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "host" not in headers


def test_list_objects_example():
    """Same page, GET Bucket (List Objects): the query is part of the signature."""
    headers = sign("GET", "examplebucket.s3.amazonaws.com", "/", {"max-keys": "2", "prefix": "J"}, {},
                   EMPTY_SHA256, ACCESS, SECRET, WHEN, region="us-east-1")
    assert signature(headers) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


def test_endpoint_and_query():
    assert endpoint("abc", "default") == "https://abc.r2.cloudflarestorage.com"
    assert endpoint("abc", "eu") == "https://abc.eu.r2.cloudflarestorage.com"
    assert query_string({"prefix": "protokoll/2026/", "list-type": "2"}) == \
        "list-type=2&prefix=protokoll%2F2026%2F"


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.locked = set()
        self.queries = []
        self.page = 2

    def app(self):
        app = web.Application()
        app.router.add_route("*", "/{bucket}", self.bucket)
        app.router.add_route("*", "/{bucket}/{key:.+}", self.object)
        return app

    def check(self, request, body=b""):
        assert request.headers["authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS}/")
        assert request.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()

    async def object(self, request):
        key = request.match_info["key"]
        body = await request.read()
        self.check(request, body)
        if request.method == "PUT":
            if key in self.locked:
                return web.Response(status=409, text="<Error><Code>ObjectLockedByBucketPolicy</Code></Error>")
            if request.headers.get("if-none-match") == "*" and key in self.objects:
                return web.Response(status=412, text="<Error><Code>PreconditionFailed</Code></Error>")
            self.objects[key] = body
            return web.Response(status=200)
        if key not in self.objects:
            return web.Response(status=404, text="<Error><Code>NoSuchKey</Code><Message>gone</Message></Error>")
        return web.Response(body=self.objects[key])

    async def bucket(self, request):
        self.check(request)
        self.queries.append(request.raw_path.split("?", 1)[1] if "?" in request.raw_path else "")
        if request.headers["authorization"].endswith("Signature=bad"):
            return web.Response(status=403)
        prefix = request.query.get("prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        start = int(request.query.get("continuation-token", "0"))
        chunk = keys[start:start + self.page]
        truncated = start + self.page < len(keys)
        ns = "http://s3.amazonaws.com/doc/2006-03-01/"
        xml = (f'<ListBucketResult xmlns="{ns}"><IsTruncated>{str(truncated).lower()}</IsTruncated>'
               + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in chunk)
               + (f"<NextContinuationToken>{start + self.page}</NextContinuationToken>" if truncated else "")
               + "</ListBucketResult>")
        return web.Response(text=xml, content_type="application/xml")


@pytest.fixture
async def s3(aiohttp_server):
    fake = FakeS3()
    server = await aiohttp_server(fake.app())
    async with aiohttp.ClientSession() as http:
        client = R2(http, "acc", "default", "bucket", ACCESS, "secret")
        client._base = f"http://{server.host}:{server.port}"
        client._host = f"{server.host}:{server.port}"
        yield fake, client


async def test_put_once(s3):
    fake, client = s3
    assert await client.put_once("protokoll/2026/10/31/a.json", b"one", "application/json") is True
    assert await client.put_once("protokoll/2026/10/31/a.json", b"two", "application/json") is False
    assert fake.objects["protokoll/2026/10/31/a.json"] == b"one"
    fake.locked.add("x")
    fake.objects["x"] = b"old"
    assert await client.put_once("x", b"new", "text/plain") is False


async def test_get_and_list_pages(s3):
    fake, client = s3
    for i in range(5):
        fake.objects[f"protokoll/2026/10/31/{i}.json"] = str(i).encode()
    fake.objects["snapshots/2026/10/31/x/meta.json"] = b"{}"
    assert await client.get("protokoll/2026/10/31/3.json") == b"3"
    keys = await client.list("protokoll/2026/10/31/")
    assert keys == [f"protokoll/2026/10/31/{i}.json" for i in range(5)]
    # The query goes out exactly as signed: slashes encoded
    assert fake.queries[0] == "list-type=2&prefix=protokoll%2F2026%2F10%2F31%2F"
    with pytest.raises(R2Error) as err:
        await client.get("missing")
    assert err.value.status == 404 and err.value.code == "NoSuchKey" and not err.value.unreachable


async def test_auth_error(s3, monkeypatch):
    _, client = s3
    import r2_archive.r2 as module
    original = module.sign

    def bad(*args, **kwargs):
        headers = original(*args, **kwargs)
        headers["authorization"] = headers["authorization"].rsplit("Signature=", 1)[0] + "Signature=bad"
        return headers

    monkeypatch.setattr(module, "sign", bad)
    with pytest.raises(R2AuthError):
        await client.list("")


async def test_unreachable():
    async with aiohttp.ClientSession() as http:
        client = R2(http, "acc", "default", "bucket", ACCESS, "secret", timeout=2)
        client._base = "http://127.0.0.1:9"
        client._host = "127.0.0.1:9"
        with pytest.raises(R2Error) as err:
            await client.put_once("k", b"v", "text/plain")
        assert err.value.unreachable
