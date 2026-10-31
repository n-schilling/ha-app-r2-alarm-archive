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
        dimensions { actionType }
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


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def parse_usage(data: dict) -> Usage:
    try:
        account = data["data"]["viewer"]["accounts"][0]
    except (KeyError, IndexError, TypeError) as err:
        raise CloudflareError(f"no R2 usage in the answer: {data.get('errors') or data}") from err
    class_a = class_b = 0
    for group in account.get("operations") or []:
        action = (group.get("dimensions") or {}).get("actionType")
        requests = int((group.get("sum") or {}).get("requests") or 0)
        if action in CLASS_A:
            class_a += requests
        elif action in CLASS_B:
            class_b += requests
    storage = sum(int((g.get("max") or {}).get("payloadSize") or 0)
                  + int((g.get("max") or {}).get("metadataSize") or 0)
                  for g in account.get("storage") or [])
    return Usage(class_a, class_b, storage)


class Cloudflare:
    def __init__(self, http: aiohttp.ClientSession, token: str, account_id: str, bucket: str,
                 jurisdiction: str, api: str = API) -> None:
        self._http = http
        self._headers = {"Authorization": f"Bearer {token}"}
        if jurisdiction != "default":
            self._headers["cf-r2-jurisdiction"] = jurisdiction
        self._account = account_id
        self._bucket = bucket
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
        return parse_usage(data)

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
