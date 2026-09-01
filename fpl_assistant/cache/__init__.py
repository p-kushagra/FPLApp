"""Stale-while-revalidate cache. Every external read goes through here.

The contract, from design doc section 5.2:

    MISS      no entry                  -> blocking fetch
    FRESH     age < soft_ttl            -> serve cached
    STALE     soft_ttl <= age < hard    -> serve cached NOW, revalidate behind
    EXPIRED   age >= hard_ttl           -> blocking fetch
    frozen    write-once tier           -> always serve cached

    fetch ok      -> write, Quality.FRESH
    fetch failed  -> any cached value at all? serve it as Quality.DEGRADED
                     nothing cached?           Quality.UNAVAILABLE

The caller branches on `result.quality`, never on try/except -- that is what
makes the degradation matrix enforceable rather than aspirational.

Background revalidation is pluggable. Phase 1 ships no job runner, so the
default revalidator is None and a STALE read simply serves the stale value.
Phase 2 calls `set_revalidator()` once at boot to wire in the job queue; nothing
else in this module changes.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from ..sources.base import Quality, SourceError, SourceResult
from . import store
from .tiers import TIERS, Tier, get_tier

__all__ = [
    "TIERS",
    "Tier",
    "clear_revalidator",
    "get_or_revalidate",
    "invalidate",
    "purge_expired",
    "set_revalidator",
    "stats",
]

log = logging.getLogger(__name__)

# Signature: (key, tier_name) -> None. Set by the job layer in Phase 2.
Revalidator = Callable[[str, str], None]
_revalidator: Revalidator | None = None


def set_revalidator(fn: Revalidator | None) -> None:
    """Install the background-refresh hook (Phase 2 wires the job queue here)."""
    global _revalidator
    _revalidator = fn


def clear_revalidator() -> None:
    set_revalidator(None)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _from_record(record: store.CacheRecord, quality: Quality, source: str,
                 now: dt.datetime, error: str | None = None) -> SourceResult:
    return SourceResult(
        data=record.data,
        quality=quality,
        source=source,
        fetched_at=record.fetched_at,
        age_seconds=record.age_seconds(now),
        error=error,
    )


def get_or_revalidate(
    conn: sqlite3.Connection,
    key: str,
    tier: str,
    fetch_fn: Callable[[], Any],
    *,
    source: str | None = None,
    force: bool = False,
    now: dt.datetime | None = None,
) -> SourceResult:
    """Serve `key` from cache, fetching or revalidating as the tier dictates.

    `fetch_fn` returns the raw decoded payload or raises. Any exception is
    caught and mapped onto a Quality -- this function never propagates one.

    `force=True` bypasses freshness (but still honours a frozen tier, which is
    immutable by construction).
    """
    moment = now or _utcnow()
    tier_spec = get_tier(tier)
    label = source or tier

    record = store.read(conn, key)

    # A frozen entry is the final word; nothing can supersede it.
    if record is not None and record.frozen:
        store.touch(conn, key)
        return _from_record(record, Quality.FRESH, label, moment)

    if record is not None and not force:
        if not record.is_soft_expired(moment):
            store.touch(conn, key)
            return _from_record(record, Quality.FRESH, label, moment)

        if not record.is_hard_expired(moment):
            # Serve immediately, refresh behind. If no revalidator is installed
            # the value is still correct to serve -- it is just labelled STALE.
            store.touch(conn, key)
            _enqueue_revalidation(key, tier)
            return _from_record(record, Quality.STALE, label, moment)

    # MISS, EXPIRED or forced: fetch synchronously.
    try:
        data = fetch_fn()
    except SourceError as exc:
        return _degraded(conn, key, record, label, moment, str(exc))
    except Exception as exc:  # noqa: BLE001 - adapters must never leak
        log.warning("cache fetch failed for %s: %s", key, exc)
        return _degraded(conn, key, record, label, moment, repr(exc))

    store.write(conn, key, tier_spec, data, now=moment)
    return SourceResult(
        data=data,
        quality=Quality.FRESH,
        source=label,
        fetched_at=moment,
        age_seconds=0.0,
    )


def _degraded(conn: sqlite3.Connection, key: str, record: store.CacheRecord | None,
              source: str, now: dt.datetime, error: str) -> SourceResult:
    """A fetch failed. Serve whatever is cached, however old, or admit defeat."""
    if record is not None:
        store.touch(conn, key)
        return _from_record(record, Quality.DEGRADED, source, now, error=error)
    return SourceResult(
        data=None,
        quality=Quality.UNAVAILABLE,
        source=source,
        fetched_at=None,
        age_seconds=None,
        error=error,
    )


def _enqueue_revalidation(key: str, tier: str) -> None:
    if _revalidator is None:
        return
    try:
        _revalidator(key, tier)
    except Exception as exc:  # noqa: BLE001
        # A queue problem must never degrade a read that already succeeded.
        log.warning("revalidation enqueue failed for %s: %s", key, exc)


def invalidate(conn: sqlite3.Connection, prefix: str) -> int:
    return store.invalidate(conn, prefix)


def purge_expired(conn: sqlite3.Connection) -> int:
    return store.purge_expired(conn)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return store.stats(conn)
