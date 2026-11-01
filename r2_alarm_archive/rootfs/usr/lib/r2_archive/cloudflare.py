"""The Cloudflare API with a read-only account token: R2 usage of the whole
account (GraphQL Analytics) and the lifecycle and lock rules of the bucket."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp

API = "https://api.cloudflare.com/client/v4"

# From developers.cloudflare.com/r2/pricing; everything else (DeleteObject,
# AbortMultipartUpload, ...) is free
CLASS_A = frozenset({
    "ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
    "CreateMultipartUpload", "LifecycleStorageTierTransition", "ListMultipartUploads", "UploadPart",
    "UploadPartCopy", "ListParts", "PutBucketEncryption", "PutBucketCors",
    "PutBucketLifecycleConfiguration",
})
CLASS_B = frozenset({
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption",
    "GetBucketLocation", "GetBucketCors", "GetBucketLifecycleConfiguration",
})

USAGE_QUERY = """
query R2Usage($account: string!, $monthStart: Time!, $dayStart: Time!, $end: Time!) {
  viewer {
    accounts(filter: {accountTag: $account}) {
      operations: r2OperationsAdaptiveGroups(
        limit: 10000, filter: {datetime_geq: $monthStart, datetime_leq: $end}) {
        sum { requests }
        dimensions { actionType bucketName }
      }
      storage: r2StorageAdaptiveGroups(
        limit: 10000, filter: {datetime_geq: $dayStart, datetime_leq: $end}) {
        max { payloadSize metadataSize }
        dimensions { bucketName }
      }
    }
  }
}
"""


class CloudflareError(Exception):
    """The API could not be reached, refused the token or answered with errors."""


@dataclass(frozen=True)
class Usage:
    """R2 usage of the whole account in the current month."""
    class_a: int
    class_b: int
    storage_bytes: int       # all buckets, highest value of the last 24 hours
    # The share of the archive's own bucket in the figures above
    bucket_class_a: int = 0
    bucket_class_b: int = 0
    bucket_storage_bytes: int = 0


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def bucket_names(bucket: str, jurisdiction: str) -> set[str]:
    """How GraphQL may name the bucket: a bucket with a jurisdiction carries it as prefix."""
    return {bucket} if jurisdiction == "default" else {bucket, f"{jurisdiction}_{bucket}"}


def parse_usage(data: dict, own: set[str] = frozenset()) -> Usage:
    try:
        account = data["data"]["viewer"]["accounts"][0]
    except (KeyError, IndexError, TypeError) as err:
        raise CloudflareError(f"no R2 usage in the answer: {data.get('errors') or data}") from err
    ops = {"a": 0, "b": 0, "own_a": 0, "own_b": 0}
    for group in account.get("operations") or []:
        dims = group.get("dimensions") or {}
        requests = int((group.get("sum") or {}).get("requests") or 0)
        kind = "a" if dims.get("actionType") in CLASS_A else "b" if dims.get("actionType") in CLASS_B else None
        if kind:
            ops[kind] += requests
            if dims.get("bucketName") in own:
                ops["own_" + kind] += requests
    storage = own_storage = 0
    for group in account.get("storage") or []:
        size = int((group.get("max") or {}).get("payloadSize") or 0) \
            + int((group.get("max") or {}).get("metadataSize") or 0)
        storage += size
        if (group.get("dimensions") or {}).get("bucketName") in own:
            own_storage += size
    return Usage(ops["a"], ops["b"], storage, ops["own_a"], ops["own_b"], own_storage)


class Cloudflare:
    def __init__(self, http: aiohttp.ClientSession, token: str, account_id: str, bucket: str,
                 jurisdiction: str, api: str = API) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        if jurisdiction != "default":
            self._headers["cf-r2-jurisdiction"] = jurisdiction
        self._account = account_id
        self._bucket = bucket
        self._jurisdiction = jurisdiction
        self._api = api
        self._timeout = aiohttp.ClientTimeout(total=30)

    async def _json(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with self._http.request(method, url, headers=self._headers, timeout=self._timeout,
                                          **kwargs) as resp:
                if resp.status in (401, 403):
                    raise CloudflareError(f"api_token refused (HTTP {resp.status}); it needs "
                                          "Account Analytics Read and Workers R2 Storage Read")
                if resp.status != 200:
                    raise CloudflareError(f"{url}: HTTP {resp.status}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CloudflareError(f"{url}: {err or type(err).__name__}") from err

    async def usage(self, now: datetime) -> Usage:
        """`now` in UTC: the month counts from the 1st, 00:00 UTC."""
        variables = {"account": self._account, "monthStart": month_start(now).isoformat(),
                     "dayStart": (now - timedelta(days=1)).isoformat(), "end": now.isoformat()}
        data = await self._json("POST", f"{self._api}/graphql",
                                json={"query": USAGE_QUERY, "variables": variables})
        if data.get("errors"):
            raise CloudflareError(f"GraphQL: {data['errors'][0].get('message', data['errors'])}")
        return parse_usage(data, bucket_names(self._bucket, self._jurisdiction))

    async def _rules(self, what: str) -> list[dict]:
        data = await self._json(
            "GET", f"{self._api}/accounts/{self._account}/r2/buckets/{self._bucket}/{what}")
        if not data.get("success", False):
            raise CloudflareError(f"{what} rules: {data.get('errors')}")
        return list((data.get("result") or {}).get("rules") or [])

    async def lifecycle_rules(self) -> list[dict]:
        return await self._rules("lifecycle")

    async def lock_rules(self) -> list[dict]:
        return await self._rules("lock")

    async def token_expiry(self) -> datetime | None:
        """When api_token expires, None when it never does. Any token may
        verify itself: an account token at the account, a user token at the
        user."""
        for url in (f"{self._api}/accounts/{self._account}/tokens/verify", f"{self._api}/user/tokens/verify"):
            try:
                async with self._http.get(url, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status in (400, 401, 403, 404):
                        continue
                    if resp.status != 200:
                        raise CloudflareError(f"{url}: HTTP {resp.status}")
                    data = await resp.json()
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise CloudflareError(f"{url}: {err or type(err).__name__}") from err
            result = data.get("result") or {}
            if not data.get("success", False) or result.get("status") != "active":
                raise CloudflareError(f"api_token not active: {result.get('status') or data.get('errors')}")
            expires = result.get("expires_on")
            if not expires:
                return None
            try:
                return datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            except ValueError as err:
                raise CloudflareError(f"api_token: unreadable expires_on {expires!r}") from err
        raise CloudflareError("api_token could not be verified")
