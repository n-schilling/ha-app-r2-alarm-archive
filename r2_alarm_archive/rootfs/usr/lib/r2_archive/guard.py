"""Keeps the whole Cloudflare account 10 % below every limit of R2's free tier.

The account's usage comes from GraphQL Analytics, which lags a few minutes;
the app's own operations since the last poll are counted on top. Without a
recent usage figure nothing passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cloudflare import Usage, month_start

# developers.cloudflare.com/r2/pricing/#free-tier, per month and account
FREE_CLASS_A = 1_000_000
FREE_CLASS_B = 10_000_000
FREE_STORAGE = 10 * 10**9          # 10 GB-month; GB counted as 10^9 bytes, the stricter reading
MARGIN = 0.9
LIMITS = {"class_a": int(FREE_CLASS_A * MARGIN), "class_b": int(FREE_CLASS_B * MARGIN),
          "storage": int(FREE_STORAGE * MARGIN)}
# A usage figure older than this does not count
MAX_AGE = 3600.0


@dataclass
class Guard:
    enabled: bool
    usage: Usage | None = None
    polled_at: float | None = None          # loop time of the last good poll
    month: tuple[int, int] | None = None    # (year, month) the usage is for
    own: dict[str, int] = field(default_factory=lambda: {"class_a": 0, "class_b": 0, "storage": 0})

    def update(self, usage: Usage, now: float, month: tuple[int, int]) -> None:
        self.usage, self.polled_at, self.month = usage, now, month
        self.own = {"class_a": 0, "class_b": 0, "storage": 0}

    def totals(self) -> dict[str, int] | None:
        if self.usage is None:
            return None
        return {"class_a": self.usage.class_a + self.own["class_a"],
                "class_b": self.usage.class_b + self.own["class_b"],
                "storage": self.usage.storage_bytes + self.own["storage"]}

    def valid(self, now: float, month: tuple[int, int]) -> bool:
        return (self.usage is not None and self.polled_at is not None
                and now - self.polled_at <= MAX_AGE and self.month == month)

    def allows(self, now: float, month: tuple[int, int], *, class_a: int = 0, class_b: int = 0,
               storage: int = 0) -> bool:
        """Whether these operations keep every figure below its limit."""
        if not self.enabled:
            return True
        if not self.valid(now, month):
            return False
        totals = self.totals()
        return (totals["class_a"] + class_a < LIMITS["class_a"]
                and totals["class_b"] + class_b < LIMITS["class_b"]
                and totals["storage"] + storage < LIMITS["storage"])

    def count(self, *, class_a: int = 0, class_b: int = 0, storage: int = 0) -> None:
        self.own["class_a"] += class_a
        self.own["class_b"] += class_b
        self.own["storage"] += storage

    def limit_reached(self, now: float, month: tuple[int, int]) -> bool | None:
        """For the sensor: True at the limit, None without a usage figure (the
        guard then holds everything back too, but no limit is known to be reached)."""
        if not self.enabled:
            return False
        if not self.valid(now, month):
            return None
        return not self.allows(now, month, class_a=1, class_b=1)

    def percent(self) -> dict[str, float] | None:
        totals = self.totals()
        if totals is None:
            return None
        free = {"class_a": FREE_CLASS_A, "class_b": FREE_CLASS_B, "storage": FREE_STORAGE}
        return {k: round(100 * totals[k] / free[k], 2) for k in free}


def utc_month(now_utc) -> tuple[int, int]:
    start = month_start(now_utc)
    return start.year, start.month
