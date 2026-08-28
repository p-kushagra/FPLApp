"""Pluggable insight providers, response caching and persistence helpers."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

from ..config import Config
from .base import Insight, InsightsProvider
from .claude_provider import ClaudeSubscriptionProvider
from .null_provider import NullProvider

__all__ = [
    "Insight",
    "InsightsProvider",
    "get_provider",
    "save_insight",
    "import_exports",
    "latest_insight",
    "cache_key_for",
    "cached_insight",
    "store_cache",
    "cache_stats",
    "summarise_cached",
]


def get_provider(cfg: Config) -> InsightsProvider:
    if cfg.insights_provider == "claude":
        return ClaudeSubscriptionProvider(cfg)
    return NullProvider()


# ---------------------------------------------------------------------------
# Response cache: the same news for the same player must never be paid for twice.
# ---------------------------------------------------------------------------
def cache_key_for(player_id: int, chunks: list[dict], provider: str) -> str:
    """Hash of the exact evidence set, so new news invalidates the entry naturally."""
    material = "|".join(sorted(str(c.get("id")) for c in chunks))
    raw = f"{provider}:{player_id}:{material}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cached_insight(conn: sqlite3.Connection, key: str) -> Insight | None:
    row = conn.execute("SELECT payload FROM ai_cache WHERE cache_key = ?", (key,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE ai_cache SET hits = hits + 1 WHERE cache_key = ?", (key,))
    conn.commit()
    try:
        return Insight(**json.loads(row["payload"]))
    except (ValueError, TypeError):
        return None


def store_cache(conn: sqlite3.Connection, key: str, insight: Insight) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO ai_cache(cache_key, player_id, payload, created_at, provider, hits)
           VALUES (?, ?, ?, ?, ?, COALESCE((SELECT hits FROM ai_cache WHERE cache_key = ?), 0))""",
        (key, insight.player_id, json.dumps(insight.__dict__),
         dt.datetime.utcnow().isoformat(), insight.provider, key),
    )
    conn.commit()


def cache_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) entries, COALESCE(SUM(hits), 0) hits FROM ai_cache"
    ).fetchone()
    return {"entries": row["entries"], "hits": row["hits"]}


def summarise_cached(conn: sqlite3.Connection, cfg: Config, provider: InsightsProvider,
                     player: dict, chunks: list[dict],
                     force: bool = False) -> tuple[Insight, bool]:
    """Return (insight, from_cache). Only calls the provider on a cache miss."""
    key = cache_key_for(player["id"], chunks, cfg.insights_provider)
    if not force:
        hit = cached_insight(conn, key)
        if hit is not None:
            return hit, True
    insight = provider.summarise(player, chunks)
    # Pending bundles are not final answers, so they are not cached.
    if insight.signal_type not in ("pending", "error"):
        store_cache(conn, key, insight)
    return insight, False


def save_insight(conn: sqlite3.Connection, insight: Insight) -> None:
    conn.execute(
        """INSERT INTO insights
           (player_id, signal_type, status, expected_return, confidence, summary,
            source_urls, created_at, provider)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            insight.player_id, insight.signal_type, insight.status,
            insight.expected_return, insight.confidence, insight.summary,
            insight.source_urls, dt.datetime.utcnow().isoformat(), insight.provider,
        ),
    )
    conn.commit()


def latest_insight(conn: sqlite3.Connection, player_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM insights WHERE player_id = ? ORDER BY created_at DESC LIMIT 1",
        (player_id,),
    ).fetchone()
    return dict(row) if row else None


def import_exports(cfg: Config, conn: sqlite3.Connection) -> int:
    """Import Claude JSON results dropped into the exports/ folder (bundle mode)."""
    imported = 0
    for path in sorted(cfg.exports_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        records = data if isinstance(data, list) else [data]
        for rec in records:
            if "player_id" not in rec:
                continue
            save_insight(conn, Insight(
                player_id=int(rec["player_id"]),
                signal_type=rec.get("signal_type", "unknown"),
                status=rec.get("status", ""),
                expected_return=rec.get("expected_return", ""),
                confidence=rec.get("confidence", ""),
                summary=rec.get("summary", ""),
                source_urls=", ".join(rec.get("sources", [])) if isinstance(rec.get("sources"), list) else rec.get("sources", ""),
                provider="claude:import",
            ))
            imported += 1
        path.rename(path.with_suffix(path.suffix + ".done"))
    return imported
