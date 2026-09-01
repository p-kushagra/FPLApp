"""The single place where HTTP failures become Quality states.

Every adapter calls `request_json` or `request_text`. Both enforce the rate
budget, retry with jittered backoff, record source health, and translate the
entire failure space into the `SourceError` taxonomy. Nothing else in the
codebase touches `requests` directly.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from urllib.parse import urlparse

import requests

from . import ratelimit
from .base import Malformed, NotFound, RateLimited, Unavailable

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_ATTEMPTS = 3
MAX_BACKOFF = 60.0


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _backoff(attempt: int) -> float:
    """Exponential with full jitter.

    Full jitter (uniform over [0, 2^n]) rather than plain exponential: when a
    fan-out of 25 workers all hit a 429 together, identical backoff makes them
    retry in lockstep and trip the limit again.
    """
    return random.uniform(0, min(MAX_BACKOFF, 2.0 ** attempt))


def record_health(conn: sqlite3.Connection, source: str, *, ok: bool,
                  latency_ms: float | None = None, error: str | None = None) -> None:
    """Update `source_health`. Three consecutive failures flip it to 'down'."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if ok:
        conn.execute(
            """INSERT INTO source_health
                 (source, last_success_at, consecutive_failures, p50_ms, quality, updated_at)
               VALUES (?, ?, 0, ?, 'ok', ?)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at = excluded.last_success_at,
                 consecutive_failures = 0,
                 p50_ms = COALESCE((source_health.p50_ms + excluded.p50_ms) / 2.0,
                                   excluded.p50_ms),
                 quality = 'ok',
                 updated_at = excluded.updated_at""",
            (source, now, latency_ms, now),
        )
    else:
        conn.execute(
            """INSERT INTO source_health
                 (source, last_failure_at, last_error, consecutive_failures,
                  quality, updated_at)
               VALUES (?, ?, ?, 1, 'degraded', ?)
               ON CONFLICT(source) DO UPDATE SET
                 last_failure_at = excluded.last_failure_at,
                 last_error = excluded.last_error,
                 consecutive_failures = source_health.consecutive_failures + 1,
                 quality = CASE
                     WHEN source_health.consecutive_failures + 1 >= 3 THEN 'down'
                     ELSE 'degraded' END,
                 updated_at = excluded.updated_at""",
            (source, now, (error or "")[:500], now),
        )
    conn.commit()


def _fetch(conn: sqlite3.Connection, session: requests.Session, url: str,
           source: str, timeout: float, headers: dict | None,
           sleep: Callable[[float], None]) -> requests.Response:
    """Rate-limited fetch with retries. Raises a SourceError on give-up."""
    host = _host(url)

    if not ratelimit.acquire(conn, host, sleep=sleep):
        raise RateLimited(f"local rate budget exhausted for {host}")

    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            resp = session.get(url, timeout=timeout, headers=headers)
        except requests.Timeout:
            last = Unavailable(f"timeout after {timeout}s: {url}")
            log.debug("timeout %s (attempt %d)", url, attempt + 1)
        except requests.RequestException as exc:
            last = Unavailable(f"connection error: {exc}")
            log.debug("connection error %s: %s", url, exc)
        else:
            latency_ms = (time.monotonic() - started) * 1000.0

            if resp.status_code == 429:
                ratelimit.record_429(conn, host)
                retry_after = _retry_after(resp)
                last = RateLimited(f"429 from {host}", retry_after=retry_after)
                if attempt < MAX_ATTEMPTS - 1:
                    sleep(retry_after if retry_after is not None else _backoff(attempt))
                    continue
            elif resp.status_code == 404:
                # Not retryable and not a system fault -- a 404 is an answer.
                record_health(conn, source, ok=True, latency_ms=latency_ms)
                raise NotFound(f"404: {url}")
            elif resp.status_code >= 500:
                last = Unavailable(f"HTTP {resp.status_code} from {host}")
            elif not resp.ok:
                record_health(conn, source, ok=False,
                              error=f"HTTP {resp.status_code}")
                raise Unavailable(f"HTTP {resp.status_code}: {url}")
            else:
                record_health(conn, source, ok=True, latency_ms=latency_ms)
                return resp

        if attempt < MAX_ATTEMPTS - 1:
            sleep(_backoff(attempt))

    record_health(conn, source, ok=False, error=str(last))
    raise last or Unavailable(f"exhausted {MAX_ATTEMPTS} attempts: {url}")


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(MAX_BACKOFF, float(raw))
    except ValueError:
        return None


def request_json(conn: sqlite3.Connection, session: requests.Session, url: str,
                 source: str, timeout: float = DEFAULT_TIMEOUT,
                 headers: dict | None = None,
                 sleep: Callable[[float], None] | None = None):
    """GET and parse JSON. Raises SourceError; the caller's cache maps it."""
    resp = _fetch(conn, session, url, source, timeout, headers, sleep or time.sleep)
    try:
        return resp.json()
    except ValueError as exc:
        record_health(conn, source, ok=False, error=f"invalid JSON: {exc}")
        raise Malformed(f"response was not JSON: {url}") from exc


def request_text(conn: sqlite3.Connection, session: requests.Session, url: str,
                 source: str, timeout: float = DEFAULT_TIMEOUT,
                 headers: dict | None = None,
                 sleep: Callable[[float], None] | None = None) -> str:
    resp = _fetch(conn, session, url, source, timeout, headers, sleep or time.sleep)
    return resp.text
