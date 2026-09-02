"""Schema v6 - saved sandbox scenarios.

The transfer sandbox is an in-memory what-if: mutating `my_picks` as you drag a
player around would destroy the record of what you actually own, which is the
one thing every retrospective is measured against. So sandbox state lives in
`st.session_state` and reaches SQLite only when the operator presses Save.

These two tables are that explicit save. They are deliberately a SEPARATE
namespace from `my_picks` / `pre_gw_projections` -- nothing in the ingest,
projection or calibration path reads them, so a saved scenario can never be
mistaken for a real squad by code that was not looking for one.

`baseline_xp` and `scenario_xp` are stored rather than recomputed on load: the
projection run they were measured against gets overwritten on the next
`recompute_xp`, and a saved comparison that silently re-baselines itself against
newer numbers is not a record of the decision you took.
"""
from __future__ import annotations

V6_TABLES = """
CREATE TABLE IF NOT EXISTS scenario (
  scenario_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  gw            INTEGER NOT NULL,
  chip          TEXT,                 -- NULL | wildcard | free_hit | bench_boost | triple_captain
  bank          REAL NOT NULL DEFAULT 0.0,
  free_transfers INTEGER NOT NULL DEFAULT 1,
  transfers     INTEGER NOT NULL DEFAULT 0,
  hit_points    INTEGER NOT NULL DEFAULT 0,
  baseline_xp   REAL,                 -- XI xP of the stored squad, at save time
  scenario_xp   REAL,                 -- XI xP of this scenario, at save time
  net_ev        REAL,                 -- scenario_xp - baseline_xp - hit_points
  run_id        TEXT,                 -- the xp_projection run both were read from
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_pick (
  scenario_id   INTEGER NOT NULL,
  player_id     INTEGER NOT NULL,
  position      TEXT,
  starting      INTEGER NOT NULL DEFAULT 0,
  bench_order   INTEGER NOT NULL DEFAULT 0,
  is_captain    INTEGER NOT NULL DEFAULT 0,
  is_vice       INTEGER NOT NULL DEFAULT 0,
  cost          REAL,                 -- price paid in this scenario
  sell_price     REAL,                -- what it would sell for, at save time
  xp            REAL,
  PRIMARY KEY (scenario_id, player_id),
  FOREIGN KEY (scenario_id) REFERENCES scenario(scenario_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_scenario_gw ON scenario(gw, created_at);
"""
