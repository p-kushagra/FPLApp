"""Schema v5 - per-shot coordinates.

`understat_player_match` stores match *totals*, which is all the xP model needs.
The shot map needs the individual events: where each attempt was taken from,
what it was worth, and whether it went in. That detail arrives free -- the
`getPlayerData` payload the per-match fan-out already fetches carries a `shots`
array alongside `matches`, so persisting it costs no extra request.

Coordinates are Understat's 0-1 fractions of the pitch, stored raw. The chart
layer decides how to project them; a table that pre-baked pixel positions would
have to be rebuilt every time the pitch drawing changed.

Keyed on the Understat shot id, which is stable and globally unique, so a
re-ingest of the same player replaces rather than duplicates.
"""
from __future__ import annotations

V5_TABLES = """
CREATE TABLE IF NOT EXISTS understat_shot (
  shot_id          TEXT PRIMARY KEY,
  understat_id     TEXT NOT NULL,      -- the player who took it
  match_id         TEXT,
  season           INTEGER,
  minute           INTEGER,
  x                REAL,               -- 0-1 fraction of pitch length
  y                REAL,               -- 0-1 fraction of pitch width
  xg               REAL,
  result           TEXT,               -- Goal | SavedShot | MissedShots | BlockedShot | ShotOnPost
  situation        TEXT,               -- OpenPlay | FromCorner | SetPiece | DirectFreekick | Penalty
  shot_type        TEXT,               -- LeftFoot | RightFoot | Head | OtherBodyPart
  last_action      TEXT,
  h_team           TEXT,
  a_team           TEXT,
  h_a              TEXT,               -- which side the shooter was on
  player_assisted  TEXT,
  match_date       TEXT,
  fetched_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_shot_player
  ON understat_shot(understat_id, season);
CREATE INDEX IF NOT EXISTS ix_shot_match ON understat_shot(match_id);
"""
