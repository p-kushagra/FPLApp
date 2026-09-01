"""Schema v3 - the pre-deadline projection freeze.

v2 stores `xp_projection`, which is a *living* table: `recompute_xp` overwrites
it whenever it runs, and every run reads whatever history exists at that moment.
That is correct for planning -- you always want the freshest forecast -- and
useless for measurement, because once a gameweek is played, any projection you
recompute for it has already seen the result.

`projection_snapshot` is the opposite contract: written once, an hour before the
deadline, never revised. It is the only table in the system that can answer
"what did we believe before we knew", which is what both the Process axis on
Page 1 and the calibration gate are built on.

It also captures FPL's own `ep_next` at the same instant. `ep_next` is a live
scalar on `players` that FPL rewrites every gameweek, so its historical values
are unrecoverable after the fact -- freezing it here is the only way the
model-vs-baseline RMSE benchmark ever becomes computable.
"""
from __future__ import annotations

V3_TABLES = """
CREATE TABLE IF NOT EXISTS projection_snapshot (
  gw                INTEGER NOT NULL,
  player_id         INTEGER NOT NULL,

  -- our forecast, component by component, exactly as xp_projection stores it
  fixtures          INTEGER,
  exp_minutes       REAL, p_start REAL, p_60 REAL,
  xp_appearance     REAL, xp_goals REAL, xp_assists REAL,
  xp_clean_sheet    REAL, xp_saves REAL, xp_defcon REAL,
  xp_bonus          REAL, xp_conceded REAL, xp_cards REAL,
  xp_total          REAL, xp_variance REAL,
  p_haul_12         REAL, p_floor_5 REAL,
  source            TEXT,

  -- the competing baseline and the market state, same instant
  ep_next           REAL,
  now_cost          REAL,
  selected_by_pct   REAL,
  status            TEXT,
  chance_of_playing INTEGER,

  -- provenance: what made this snapshot, and how early it was taken
  run_id            TEXT,
  deadline_time     TEXT,
  frozen_at         TEXT,
  lead_minutes      REAL,
  deadline_source   TEXT,

  PRIMARY KEY (gw, player_id)
);
CREATE INDEX IF NOT EXISTS ix_snapshot_gw ON projection_snapshot(gw, xp_total DESC);

-- One row per gameweek recording that the freeze happened, so a caller can ask
-- "is GW7 frozen" without counting player rows, and so a partial capture is
-- distinguishable from a complete one.
CREATE TABLE IF NOT EXISTS projection_snapshot_meta (
  gw               INTEGER PRIMARY KEY,
  run_id           TEXT,
  rows             INTEGER,
  deadline_time    TEXT,
  deadline_source  TEXT,
  frozen_at        TEXT,
  lead_minutes     REAL,
  understat_ok     INTEGER,
  note             TEXT
);

-- Calibration results. Kept as history rather than a single current row so a
-- regression in model quality is visible as a trend, not just a worse number.
CREATE TABLE IF NOT EXISTS calibration_run (
  run_id           TEXT PRIMARY KEY,
  created_at       TEXT,
  gws              TEXT,      -- JSON list of evaluated gameweeks
  n_rows           INTEGER,
  rmse_model       REAL,
  mae_model        REAL,
  bias_model       REAL,
  spearman_model   REAL,
  baseline_name    TEXT,
  rmse_baseline    REAL,
  mae_baseline     REAL,
  decile_monotonic INTEGER,
  decile_spearman  REAL,
  passed           INTEGER,
  blockers         TEXT,      -- JSON list of human-readable gate failures
  detail           TEXT       -- JSON: full metric block incl. decile table
);

-- An affine recalibration (actual ~= a + b * xp), fitted per position. Stored
-- rather than applied in place so the raw projection stays auditable, and
-- gated on sample size so a one-fold fit cannot silently take effect.
CREATE TABLE IF NOT EXISTS calibration_fit (
  position         TEXT PRIMARY KEY,
  intercept        REAL,
  slope            REAL,
  n_rows           INTEGER,
  n_gws            INTEGER,
  rmse_before      REAL,
  rmse_after       REAL,
  applied          INTEGER,   -- 0 = fitted but withheld (insufficient sample)
  run_id           TEXT,
  fitted_at        TEXT
);
"""
