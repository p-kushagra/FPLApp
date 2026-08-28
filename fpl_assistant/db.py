"""SQLite storage layer with an FTS5 full-text index for news search."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = r"""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY,
  name TEXT, short_name TEXT, strength INTEGER
);

CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY,
  web_name TEXT, first_name TEXT, second_name TEXT,
  team_id INTEGER, element_type INTEGER, position TEXT,
  now_cost REAL, selected_by_percent REAL, form REAL,
  points_per_game REAL, total_points INTEGER, status TEXT,
  chance_of_playing_next_round INTEGER,
  transfers_in_event INTEGER, transfers_out_event INTEGER,
  news TEXT, news_added TEXT
);

CREATE TABLE IF NOT EXISTS fixtures (
  id INTEGER PRIMARY KEY,
  event INTEGER, team_h INTEGER, team_a INTEGER,
  team_h_difficulty INTEGER, team_a_difficulty INTEGER,
  kickoff_time TEXT, finished INTEGER
);

CREATE TABLE IF NOT EXISTS my_picks (
  gw INTEGER, player_id INTEGER, position INTEGER,
  multiplier INTEGER, is_captain INTEGER, is_vice INTEGER,
  PRIMARY KEY (gw, player_id)
);

CREATE TABLE IF NOT EXISTS top_owned (
  gw INTEGER, player_id INTEGER,
  ownership_pct REAL, captain_pct REAL, sample_size INTEGER,
  PRIMARY KEY (gw, player_id)
);

CREATE TABLE IF NOT EXISTS news_articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, url TEXT UNIQUE, title TEXT,
  published_at TEXT, fetched_at TEXT, raw_text TEXT
);

CREATE TABLE IF NOT EXISTS news_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER, chunk_index INTEGER, text TEXT,
  published_at TEXT, source TEXT, url TEXT
);

CREATE TABLE IF NOT EXISTS news_chunk_players (
  chunk_id INTEGER, player_id INTEGER, match_score REAL,
  PRIMARY KEY (chunk_id, player_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS news_chunks_fts
  USING fts5(text, content='news_chunks', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS news_chunks_ai AFTER INSERT ON news_chunks BEGIN
  INSERT INTO news_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS news_chunks_ad AFTER DELETE ON news_chunks BEGIN
  INSERT INTO news_chunks_fts(news_chunks_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER, signal_type TEXT, status TEXT,
  expected_return TEXT, confidence TEXT, summary TEXT,
  source_urls TEXT, created_at TEXT, provider TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise RuntimeError(
                "Your Python's SQLite was built without FTS5. Install a Python with FTS5 "
                "support (standard python.org builds include it)."
            ) from exc
        raise
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def current_gw(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "current_gw", "1") or 1)
