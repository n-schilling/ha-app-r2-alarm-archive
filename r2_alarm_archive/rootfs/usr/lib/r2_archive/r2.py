"""The R2 bucket through its S3 API, signed with AWS Signature Version 4.

Only what the archive needs: create an object once (If-None-Match: *), read
one, and list a prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp
import yarl

REGION = "auto"
SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class R2Error(Exception):
    """R2 could not be reached or answered with an error."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def unreachable(self) -> bool:
        """The network or R2 itself failed, not the request (for the connection sensor)."""
        return self.status is None or self.status >= 500


class R2AuthError(R2Error):
    """The access key was refused."""


def endpoint(account_id: str, jurisdiction: str) -> str:
    zone = "" if jurisdiction == "default" else f".{jurisdiction}"
    return f"https://{account_id}{zone}.r2.cloudflarestorage.com"


def query_string(query: Mapping[str, str]) -> str:
    """The query exactly as signed; sent as is, so nothing re-encodes it."""
    return "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: Mapping[str, str], headers: Mapping[str, str],
         payload_sha256: str, access_key: str, secret_key: str, now: datetime,
         region: str = REGION) -> dict[str, str]:
    """The headers of a signed request: the given ones plus host, x-amz-date,
    x-amz-content-sha256 and Authorization. `path` is already URI-encoded."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    all_headers = {k.lower(): str(v).strip() for k, v in headers.items()}
    all_headers.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256})
    names = sorted(all_headers)
    canonical_query = query_string(query)
    canonical = "\n".join([
        method, path, canonical_query,
        "".join(f"{n}:{all_headers[n]}\n" for n in names),
        ";".join(names), payload_sha256,
    ])
    scope = f"{day}/{region}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), SERVICE), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                    f"SignedHeaders={';'.join(names)}, Signature={signature}")
    del all_headers["host"]          # aiohttp sets it from the URL
    return all_headers


def _error(status: int, body: bytes) -> tuple[str | None, str]:
    try:
        root = ET.fromstring(body)
        return root.findtext("Code"), root.findtext("Message") or ""
    except ET.ParseError:
        return None, body[:200].decode(errors="replace")


class R2:
    def __init__(self, http: aiohttp.ClientSession, account_id: str, jurisdiction: str, bucket: str,
                 access_key: str, secret_key: str, timeout: float = 60) -> None:
        self._http = http
        self._base = endpoint(account_id, jurisdiction)
        self._host = self._base.removeprefix("https://")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _path(self, key: str = "") -> str:
        return "/" + quote(self._bucket, safe="") + ("/" + quote(key, safe="/-_.~") if key else "")

    async def _request(self, method: str, key: str = "", query: Mapping[str, str] | None = None,
                       body: bytes = b"", headers: Mapping[str, str] | None = None) -> tuple[int, bytes]:
        query = dict(query or {})
        path = self._path(key)
        signed = sign(method, self._host, path, query, dict(headers or {}),
                      hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256,
                      self._access_key, self._secret_key, datetime.now(UTC))
        url = yarl.URL(self._base + path + (f"?{query_string(query)}" if query else ""), encoded=True)
        try:
            async with self._http.request(method, url, data=body or None,
                                          headers=signed, timeout=self._timeout) as resp:
                return resp.status, await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise R2Error(f"{method} {key or self._bucket}: {err or type(err).__name__}") from err

    def _raise(self, method: str, key: str, status: int, body: bytes) -> None:
        code, message = _error(status, body)
        text = f"{method} {key or self._bucket}: HTTP {status} {code or ''} {message}".strip()
        if status in (401, 403):
            raise R2AuthError(text, status, code)
        raise R2Error(text, status, code)

    async def put_once(self, key: str, body: bytes, content_type: str) -> bool:
        """Creates the object unless it exists. True: created now; False: it
        was there already (412), or it is there and locked (409)."""
        status, data = await self._request("PUT", key, body=body, headers={
            "content-type": content_type, "if-none-match": "*"})
        if status in (200, 201):
            return True
        if status == 412:
            return False
        if status == 409 and _error(status, data)[0] == "ObjectLockedByBucketPolicy":
            return False
        self._raise("PUT", key, status, data)
        return False

    async def check(self) -> None:
        """Whether R2 answers and takes the key: HeadBucket, one Class B operation."""
        status, data = await self._request("HEAD")
        if status != 200:
            self._raise("HEAD", "", status, data)

    async def get(self, key: str) -> bytes:
        status, data = await self._request("GET", key)
        if status != 200:
            self._raise("GET", key, status, data)
        return data

    async def list(self, prefix: str) -> list[str]:
        """All keys under a prefix; one Class A operation per page of 1000."""
        keys: list[str] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            status, data = await self._request("GET", query=query)
            if status != 200:
                self._raise("LIST", prefix, status, data)
            root = ET.fromstring(data)
            keys += [el.findtext(f"{S3_NS}Key") or "" for el in root.iter(f"{S3_NS}Contents")]
            if root.findtext(f"{S3_NS}IsTruncated") != "true":
                return keys
            token = root.findtext(f"{S3_NS}NextContinuationToken")
            if not token:
                return keys
