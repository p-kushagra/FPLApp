"""Persistence for the SWR cache: read, write and purge `cache_entry` rows.

Payloads are gzipped JSON. A full bootstrap-static response is ~2.5 MB of JSON
and compresses to roughly a tenth of that; at a few hundred cached responses the
difference decides whether the database stays inside its 400 MB budget.

This module knows nothing about freshness policy -- that is `cache/__init__.py`.
It only stores bytes and timestamps.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .tiers import Tier


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Rows written before this module standardised on tz-aware timestamps.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class CacheRecord:
    """A stored entry plus the derived age the freshness policy needs."""

    key: str
    tier: str
    data: Any
    fetched_at: dt.datetime
    soft_expires_at: dt.datetime | None
    hard_expires_at: dt.datetime | None
    frozen: bool
    etag: str | None
    hits: int

    def age_seconds(self, now: dt.datetime | None = None) -> float:
        return ((now or _utcnow()) - self.fetched_at).total_seconds()

    def is_soft_expired(self, now: dt.datetime | None = None) -> bool:
        if self.frozen or self.soft_expires_at is None:
            return False
        return (now or _utcnow()) >= self.soft_expires_at

    def is_hard_expired(self, now: dt.datetime | None = None) -> bool:
        if self.frozen or self.hard_expires_at is None:
            return False
        return (now or _utcnow()) >= self.hard_expires_at


def encode(data: Any) -> bytes:
    return gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def decode(blob: bytes) -> Any:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def read(conn: sqlite3.Connection, key: str) -> CacheRecord | None:
    """Return the stored entry, or None if absent or undecodable.

    A corrupt payload is treated as a miss and deleted rather than raised: a
    single bad row must not make a page unrenderable when refetching costs one
    request.
    """
    row = conn.execute(
        """SELECT cache_key, tier, payload, etag, fetched_at,
                  soft_expires_at, hard_expires_at, frozen, hits
           FROM cache_entry WHERE cache_key = ?""",
        (key,),
    ).fetchone()
    if row is None:
        return None

    fetched = _parse(row["fetched_at"])
    if fetched is None:
        return None

    try:
        data = decode(row["payload"]) if row["payload"] is not None else None
    except (OSError, ValueError, json.JSONDecodeError):
        conn.execute("DELETE FROM cache_entry WHERE cache_key = ?", (key,))
        conn.commit()
        return None

    return CacheRecord(
        key=row["cache_key"],
        tier=row["tier"],
        data=data,
        fetched_at=fetched,
        soft_expires_at=_parse(row["soft_expires_at"]),
        hard_expires_at=_parse(row["hard_expires_at"]),
        frozen=bool(row["frozen"]),
        etag=row["etag"],
        hits=int(row["hits"] or 0),
    )


def write(conn: sqlite3.Connection, key: str, tier: Tier, data: Any,
          etag: str | None = None, now: dt.datetime | None = None) -> None:
    """Store a value. A frozen tier refuses to overwrite an existing entry."""
    moment = now or _utcnow()

    if tier.frozen:
        existing = conn.execute(
            "SELECT 1 FROM cache_entry WHERE cache_key = ?", (key,)
        ).fetchone()
        if existing:
            return  # ADR-005: a frozen fact is written once

    blob = encode(data)
    conn.execute(
        """INSERT OR REPLACE INTO cache_entry
             (cache_key, tier, payload, etag, fetched_at,
              soft_expires_at, hard_expires_at, frozen, hits, bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                   COALESCE((SELECT hits FROM cache_entry WHERE cache_key = ?), 0), ?)""",
        (
            key, tier.name, blob, etag, _iso(moment),
            _iso(moment + dt.timedelta(seconds=tier.soft_ttl)),
            _iso(moment + dt.timedelta(seconds=tier.hard_ttl)),
            1 if tier.frozen else 0,
            key, len(blob),
        ),
    )
    conn.commit()


def touch(conn: sqlite3.Connection, key: str) -> None:
    """Record a cache hit. Cheap enough to call on every served read."""
    conn.execute(
        "UPDATE cache_entry SET hits = hits + 1 WHERE cache_key = ?", (key,)
    )
    conn.commit()


def invalidate(conn: sqlite3.Connection, prefix: str) -> int:
    """Drop every non-frozen entry whose key starts with `prefix`.

    Frozen entries survive deliberately -- invalidating a frozen rival squad
    would silently change a gameweek's ILEO denominator after the fact.
    """
    cur = conn.execute(
        "DELETE FROM cache_entry WHERE cache_key LIKE ? AND frozen = 0",
        (f"{prefix}%",),
    )
    conn.commit()
    return cur.rowcount


def purge_expired(conn: sqlite3.Connection, now: dt.datetime | None = None) -> int:
    cur = conn.execute(
        "DELETE FROM cache_entry WHERE frozen = 0 AND hard_expires_at < ?",
        (_iso(now or _utcnow()),),
    )
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """SELECT COUNT(*) AS entries, COALESCE(SUM(bytes), 0) AS bytes,
                  COALESCE(SUM(hits), 0) AS hits,
                  COALESCE(SUM(frozen), 0) AS frozen
           FROM cache_entry"""
    ).fetchone()
    by_tier = {
        r["tier"]: {"entries": r["n"], "bytes": r["b"]}
        for r in conn.execute(
            "SELECT tier, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b "
            "FROM cache_entry GROUP BY tier"
        )
    }
    return {
        "entries": row["entries"],
        "bytes": row["bytes"],
        "hits": row["hits"],
        "frozen": row["frozen"],
        "by_tier": by_tier,
    }
