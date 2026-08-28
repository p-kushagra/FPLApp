"""Pluggable insight providers and persistence helpers."""
from __future__ import annotations

import datetime as dt
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
]


def get_provider(cfg: Config) -> InsightsProvider:
    if cfg.insights_provider == "claude":
        return ClaudeSubscriptionProvider(cfg)
    return NullProvider()


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
