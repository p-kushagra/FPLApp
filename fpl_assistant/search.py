"""Keyword/full-text retrieval over news chunks (FTS5, no models)."""
from __future__ import annotations

import re
import sqlite3

INTENT_KEYWORDS = [
    "injury", "injured", "doubt", "doubtful", "knock", "illness", "ill",
    "fitness", "training", "rotation", "rested", "suspended", "suspension",
    "return", "fit", "setback", "scan",
]


def _fts_query(terms: list[str]) -> str:
    cleaned = []
    for term in terms:
        term = re.sub(r"[^a-zA-Z0-9 ]", " ", term).strip()
        if term:
            cleaned.append('"' + term + '"')
    return " OR ".join(cleaned) if cleaned else '""'


def player_aliases(conn: sqlite3.Connection, player_id: int) -> list[str]:
    row = conn.execute(
        "SELECT web_name, first_name, second_name FROM players WHERE id = ?",
        (player_id,),
    ).fetchone()
    if not row:
        return []
    aliases = [row["web_name"], row["second_name"]]
    if row["first_name"] and row["second_name"]:
        aliases.append(f"{row['first_name']} {row['second_name']}")
    return [a for a in aliases if a]


def search_player_news(conn: sqlite3.Connection, player_id: int, limit: int = 25) -> list[dict]:
    """Return the most recent tagged news chunks for a player."""
    rows = conn.execute(
        """SELECT c.id, c.text, c.source, c.url, c.published_at, cp.match_score
           FROM news_chunk_players cp
           JOIN news_chunks c ON c.id = cp.chunk_id
           WHERE cp.player_id = ?
           ORDER BY c.published_at DESC, cp.match_score DESC
           LIMIT ?""",
        (player_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_text(conn: sqlite3.Connection, query: str, limit: int = 25) -> list[dict]:
    """Free-text BM25 search across all news chunks."""
    rows = conn.execute(
        """SELECT c.id, c.text, c.source, c.url, c.published_at,
                  bm25(news_chunks_fts) AS score
           FROM news_chunks_fts
           JOIN news_chunks c ON c.id = news_chunks_fts.rowid
           WHERE news_chunks_fts MATCH ?
           ORDER BY score
           LIMIT ?""",
        (_fts_query([query]), limit),
    ).fetchall()
    return [dict(r) for r in rows]
