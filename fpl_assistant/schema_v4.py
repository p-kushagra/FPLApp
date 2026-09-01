"""Schema v4 - historical baselines for Bayesian prior blending.

Two gameweeks into a season, every per-90 rate in `player_gw` is noise: a
striker with one tap-in projects like Haaland and a rested starter projects
like a bench body. The fix is not a cleverer estimator over N=2, it is more N,
and the only honest place to get it is last season.

`historical_player_baselines` stores one row per player per source season:
per-90 attacking rates, the clean-sheet rate and the defensive-contribution
rate, plus the minutes behind them so the read path can refuse a 90-minute
cameo season as evidence. Rows are stored *raw* - the Championship haircut and
any source preference are applied at read time by `models.priors`, so the
table remains an auditable record of what was ingested, not of what some
version of the model decided to believe.

`pre_gw_projections` is a compatibility view over `projection_snapshot`, the
v3 write-once pre-deadline freeze. The storage contract in the v2 design names
the relation `pre_gw_projections`; the physical table keeps its v3 name so
nothing that already reads or writes it moves. A view costs nothing and keeps
both names true.
"""
from __future__ import annotations

V4_TABLES = """
CREATE TABLE IF NOT EXISTS historical_player_baselines (
  player_id          INTEGER NOT NULL,
  season_name        TEXT NOT NULL,     -- '2025/26', or 'imputed' for the matrix fallback
  source             TEXT NOT NULL,     -- 'fpl_history' | 'understat' | 'imputed'
  competition        TEXT NOT NULL DEFAULT 'PL',  -- 'PL' | 'CHAMPIONSHIP' | 'OTHER'

  total_minutes      REAL,
  npxg90_prior       REAL,   -- true npxG/90 from Understat; xG/90 from FPL history (pens included, see priors.py)
  xa90_prior         REAL,
  xcs_rate_prior     REAL,   -- clean sheets per 90 played
  defcon_rate_prior  REAL,   -- defensive-contribution actions per 90

  ingested_at        TEXT,
  PRIMARY KEY (player_id, season_name, source)
);
CREATE INDEX IF NOT EXISTS ix_baselines_player
  ON historical_player_baselines(player_id, season_name DESC);

CREATE VIEW IF NOT EXISTS pre_gw_projections AS
  SELECT * FROM projection_snapshot;
"""
