"""Per-host token bucket, persisted so limits survive a restart.

The FPL API's ~100 req/min ceiling is community-observed, not documented, so the
default budget is deliberately below it (60/min). Understat has no published
limit at all and is scraped, so it gets 20/min and a burst of 3.

Persistence matters more than it looks: without it, restarting Streamlit (which
happens on every code edit) resets the bucket and a fan-out that was pacing
itself correctly suddenly bursts.

Acquisition blocks, but never indefinitely -- past `max_wait` it gives up and
the caller returns RateLimited. A UI thread must always get an answer.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_MAX_WAIT = 30.0

# Process-wide lock. SQLite serialises writes anyway, but the read-modify-write
# of a bucket must be atomic against other threads in this process too.
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Bucket:
    capacity: float       # burst size
    refill_per_sec: float

    @classmethod
    def per_minute(cls, rate: float, burst: float) -> Bucket:
        return cls(capacity=burst, refill_per_sec=rate / 60.0)


BUCKETS: dict[str, Bucket] = {
    "fantasy.premierleague.com": Bucket.per_minute(rate=60, burst=10),
    "understat.com": Bucket.per_minute(rate=20, burst=3),
}

DEFAULT_BUCKET = Bucket.per_minute(rate=30, burst=5)


def bucket_for(host: str) -> Bucket:
    return BUCKETS.get(host, DEFAULT_BUCKET)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _load(conn: sqlite3.Connection, host: str, spec: Bucket,
          now: dt.datetime) -> tuple[float, dt.datetime]:
    """Current token count for `host`, refilled up to `now`."""
    row = conn.execute(
        "SELECT tokens, capacity, refill_per_sec, last_refill_at "
        "FROM rate_budget WHERE host = ?",
        (host,),
    ).fetchone()

    if row is None:
        return spec.capacity, now

    last = _parse(row["last_refill_at"]) or now
    elapsed = max(0.0, (now - last).total_seconds())
    tokens = min(spec.capacity, float(row["tokens"] or 0.0) + elapsed * spec.refill_per_sec)
    return tokens, now


def _save(conn: sqlite3.Connection, host: str, spec: Bucket, tokens: float,
          now: dt.datetime, *, spent: bool, was_429: bool = False) -> None:
    conn.execute(
        """INSERT INTO rate_budget
             (host, tokens, capacity, refill_per_sec, last_refill_at,
              total_requests, total_429)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(host) DO UPDATE SET
             tokens = excluded.tokens,
             capacity = excluded.capacity,
             refill_per_sec = excluded.refill_per_sec,
             last_refill_at = excluded.last_refill_at,
             total_requests = rate_budget.total_requests + ?,
             total_429 = rate_budget.total_429 + ?""",
        (
            host, tokens, spec.capacity, spec.refill_per_sec, now.isoformat(),
            1 if spent else 0, 1 if was_429 else 0,
            1 if spent else 0, 1 if was_429 else 0,
        ),
    )
    conn.commit()


def try_acquire(conn: sqlite3.Connection, host: str,
                now: dt.datetime | None = None) -> bool:
    """Take one token if available. Never blocks. Returns False if empty."""
    spec = bucket_for(host)
    moment = now or _utcnow()
    with _LOCK:
        tokens, moment = _load(conn, host, spec, moment)
        if tokens < 1.0:
            _save(conn, host, spec, tokens, moment, spent=False)
            return False
        _save(conn, host, spec, tokens - 1.0, moment, spent=True)
        return True


def acquire(conn: sqlite3.Connection, host: str,
            max_wait: float = DEFAULT_MAX_WAIT,
            sleep: Callable[[float], None] | None = None) -> bool:
    """Block until a token is available or `max_wait` elapses.

    `sleep` is injectable so tests can drive this without real time passing.
    """
    doze = sleep or time.sleep
    spec = bucket_for(host)
    deadline = time.monotonic() + max_wait

    while True:
        if try_acquire(conn, host):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Wait exactly as long as one token takes to accrue, and no longer than
        # the caller's remaining patience.
        wait = min(remaining, 1.0 / spec.refill_per_sec if spec.refill_per_sec else remaining)
        doze(max(0.01, wait))


def record_429(conn: sqlite3.Connection, host: str) -> None:
    """Halve the bucket after a 429. Upstream disagrees with our budget."""
    spec = bucket_for(host)
    now = _utcnow()
    with _LOCK:
        tokens, now = _load(conn, host, spec, now)
        _save(conn, host, spec, tokens / 2.0, now, spent=False, was_429=True)


def budget_state(conn: sqlite3.Connection, host: str) -> dict:
    """Snapshot for the Refresh Config request-budget gauge."""
    spec = bucket_for(host)
    now = _utcnow()
    tokens, _ = _load(conn, host, spec, now)
    row = conn.execute(
        "SELECT total_requests, total_429 FROM rate_budget WHERE host = ?", (host,)
    ).fetchone()
    return {
        "host": host,
        "tokens": round(tokens, 2),
        "capacity": spec.capacity,
        "per_minute": round(spec.refill_per_sec * 60, 1),
        "total_requests": int(row["total_requests"]) if row else 0,
        "total_429": int(row["total_429"]) if row else 0,
    }


def reset(conn: sqlite3.Connection, host: str | None = None) -> None:
    """Clear budget state. For tests and for the operator's 'reset' button."""
    if host:
        conn.execute("DELETE FROM rate_budget WHERE host = ?", (host,))
    else:
        conn.execute("DELETE FROM rate_budget")
    conn.commit()
