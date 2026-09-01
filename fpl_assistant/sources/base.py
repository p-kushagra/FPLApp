"""The contract every external source obeys.

One rule, and it is the load-bearing one for the whole resilience design:

    NO EXCEPTION ESCAPES A SOURCE ADAPTER.

Everything -- a 429, a timeout, a DNS failure, malformed markup, a schema change
upstream -- is mapped onto a `Quality` and returned inside a `SourceResult`.
Consumers branch on `result.quality`; they never wrap adapter calls in
try/except. That is what turns the degradation matrix (design doc 5.3) from a
document into something the type system nudges you toward.

`SourceError` and its subclasses exist for adapters to raise *internally*, so
the wrapper at the boundary can map them onto the right Quality. They are not
part of the public surface.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Quality(str, Enum):
    """How much you should trust what you just got back."""

    FRESH = "fresh"              # fetched now, or cached within its soft TTL
    STALE = "stale"              # cached past soft TTL; revalidation requested
    DEGRADED = "degraded"        # fetch failed; an older cached value is served
    UNAVAILABLE = "unavailable"  # nothing at all; the consumer must fall back

    @property
    def usable(self) -> bool:
        return self is not Quality.UNAVAILABLE

    @property
    def is_degraded(self) -> bool:
        """True when the UI owes the operator a visible badge."""
        return self in (Quality.DEGRADED, Quality.UNAVAILABLE)

    @property
    def severity(self) -> int:
        """Ordering for 'how bad is this', 0 = best.

        Needed because the enum values are strings and sort alphabetically,
        which would rank DEGRADED as better than FRESH. When a result is
        assembled from several fetches (a paged league walk, a fan-out), the
        combined quality is the worst of its parts.
        """
        return _SEVERITY[self]


_SEVERITY = {
    Quality.FRESH: 0,
    Quality.STALE: 1,
    Quality.DEGRADED: 2,
    Quality.UNAVAILABLE: 3,
}


# --------------------------------------------------------------------------
# Internal error taxonomy. Raised inside adapters, never seen by consumers.
# --------------------------------------------------------------------------
class SourceError(Exception):
    """Base for every recoverable upstream failure."""

    quality = Quality.DEGRADED


class RateLimited(SourceError):
    """429, or our own token bucket refused to issue a token in time."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Unavailable(SourceError):
    """5xx, timeout, connection reset, DNS failure."""


class Malformed(SourceError):
    """The response arrived but is not what we expect.

    Raised loudly rather than returning partial data. An Understat markup change
    that silently yielded empty stats would poison the xP model with zeros --
    far worse than an outage, because nothing would look wrong.
    """


class NotFound(SourceError):
    """404. Usually a deleted entry or a player id that no longer exists."""

    quality = Quality.UNAVAILABLE


@dataclass(frozen=True)
class SourceResult:
    """A value plus everything needed to decide how much to trust it."""

    data: Any | None
    quality: Quality
    source: str
    fetched_at: dt.datetime | None = None
    age_seconds: float | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.data is not None

    @property
    def age_minutes(self) -> float | None:
        return None if self.age_seconds is None else self.age_seconds / 60.0

    def unwrap(self, default: Any = None) -> Any:
        """Data, or `default` when there is none. The safe accessor."""
        return self.data if self.data is not None else default

    def badge(self) -> str | None:
        """Short UI label, or None when the panel needs no annotation."""
        if self.quality is Quality.FRESH:
            return None
        if self.quality is Quality.STALE:
            mins = self.age_minutes
            return f"Updating - data {int(mins)}m old" if mins else "Updating"
        if self.quality is Quality.DEGRADED:
            mins = self.age_minutes
            age = f" ({int(mins)}m old)" if mins else ""
            return f"{self.source} unavailable - showing cached{age}"
        return f"{self.source} unavailable"

    @classmethod
    def ok(cls, data: Any, source: str, **kw: Any) -> SourceResult:
        return cls(data=data, quality=Quality.FRESH, source=source,
                   fetched_at=dt.datetime.now(dt.timezone.utc), age_seconds=0.0, **kw)

    @classmethod
    def unavailable(cls, source: str, error: str) -> SourceResult:
        return cls(data=None, quality=Quality.UNAVAILABLE, source=source, error=error)
