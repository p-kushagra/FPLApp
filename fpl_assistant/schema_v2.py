"""Schema v2 DDL: the tables that turn the dashboard into a decision engine.

Kept out of `db.py` so the v1 schema stays readable and the migration ladder has
one obvious place to point at. Every statement is `IF NOT EXISTS`, so applying
this script twice is a no-op -- which is what makes the ladder in `db.migrate()`
safe to re-run after a partial failure.

Grouped exactly as CHANGE_REQUEST.md section 4.2 lists them.
"""
from __future__ import annotations

V2_TABLES = r"""
-- Temporal state -----------------------------------------------------------
-- One row per gameweek: the single source of truth for the state machine.
CREATE TABLE IF NOT EXISTS gw_state (
  gw               INTEGER PRIMARY KEY,
  deadline_time    TEXT,
  is_current       INTEGER DEFAULT 0,
  is_next          INTEGER DEFAULT 0,
  finished         INTEGER DEFAULT 0,
  data_checked     INTEGER DEFAULT 0,
  average_score    INTEGER,
  highest_score    INTEGER,
  most_captained   INTEGER,
  chip_plays       TEXT,
  transfers_made   INTEGER,
  phase            TEXT,
  updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_gw_state_phase ON gw_state(phase);

-- Free-transfer bank. Recurrence: f[t+1] = min(5, f[t] - q[t] + 1).
CREATE TABLE IF NOT EXISTS ft_bank (
  gw                   INTEGER PRIMARY KEY,
  ft_available         INTEGER NOT NULL,
  transfers_made       INTEGER DEFAULT 0,
  ft_consumed          INTEGER DEFAULT 0,
  hits                 INTEGER DEFAULT 0,
  chip_active          TEXT,
  event_transfers_cost INTEGER,
  derived              INTEGER DEFAULT 0,
  updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS chip_state (
  chip             TEXT PRIMARY KEY,
  available        INTEGER DEFAULT 1,
  played_gw        INTEGER,
  points_gained    INTEGER,
  updated_at       TEXT
);

-- Understat and entity resolution -------------------------------------------
CREATE TABLE IF NOT EXISTS entity_map (
  fpl_player_id    INTEGER PRIMARY KEY,
  understat_id     TEXT,
  understat_name   TEXT,
  understat_team   TEXT,
  confidence       REAL,
  method           TEXT,
  status           TEXT DEFAULT 'resolved',
  runner_up_score  REAL,
  source_hash      TEXT,
  resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_entity_map_status ON entity_map(status);
-- One Understat player binds to at most one FPL player. This partial unique
-- index is the structural guard against the silent mis-binding in risk R2.
CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_map_us ON entity_map(understat_id)
  WHERE understat_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS understat_player (
  understat_id     TEXT, season INTEGER,
  player_name      TEXT, team_title TEXT, position TEXT,
  games            INTEGER, time_min INTEGER,
  goals            INTEGER, assists INTEGER, shots INTEGER, key_passes INTEGER,
  xg               REAL, xa REAL, npg INTEGER, npxg REAL,
  xg_chain         REAL, xg_buildup REAL,
  yellow_cards     INTEGER, red_cards INTEGER,
  fetched_at       TEXT,
  PRIMARY KEY (understat_id, season)
);

CREATE TABLE IF NOT EXISTS understat_player_match (
  understat_id     TEXT, match_id TEXT,
  season           INTEGER, match_date TEXT,
  team_title       TEXT, opponent_title TEXT, is_home INTEGER,
  minutes          INTEGER, position TEXT,
  goals            INTEGER, assists INTEGER, shots INTEGER, key_passes INTEGER,
  xg               REAL, xa REAL, npg INTEGER, npxg REAL,
  xg_chain         REAL, xg_buildup REAL,
  fpl_gw           INTEGER,
  fetched_at       TEXT,
  PRIMARY KEY (understat_id, match_id)
);
CREATE INDEX IF NOT EXISTS ix_uspm_gw ON understat_player_match(fpl_gw);

CREATE TABLE IF NOT EXISTS understat_team (
  team_title       TEXT, season INTEGER,
  fpl_team_id      INTEGER,
  games            INTEGER, xg REAL, xga REAL, npxg REAL, npxga REAL,
  deep             INTEGER, deep_allowed INTEGER, ppda REAL, ppda_allowed REAL,
  fetched_at       TEXT,
  PRIMARY KEY (team_title, season)
);

-- Mini-league ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS league (
  league_id        INTEGER PRIMARY KEY,
  name             TEXT, league_type TEXT,
  my_rank          INTEGER, my_last_rank INTEGER, entry_count INTEGER,
  tracked          INTEGER DEFAULT 1,
  updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS league_standing (
  league_id        INTEGER, gw INTEGER, entry_id INTEGER,
  player_name      TEXT, entry_name TEXT,
  rank             INTEGER, last_rank INTEGER,
  event_total      INTEGER, total INTEGER,
  is_rival         INTEGER DEFAULT 0,
  fetched_at       TEXT,
  PRIMARY KEY (league_id, gw, entry_id)
);
CREATE INDEX IF NOT EXISTS ix_standing_rival ON league_standing(league_id, gw, is_rival);

-- Frozen rival squads. Immutable once frozen = 1 (ADR-005).
CREATE TABLE IF NOT EXISTS league_rival_pick (
  entry_id         INTEGER, gw INTEGER, player_id INTEGER,
  position         INTEGER, multiplier INTEGER,
  is_captain       INTEGER, is_vice INTEGER,
  chip             TEXT,
  frozen           INTEGER DEFAULT 0,
  frozen_at        TEXT,
  PRIMARY KEY (entry_id, gw, player_id)
);
CREATE INDEX IF NOT EXISTS ix_rival_pick_gw ON league_rival_pick(gw, player_id);

CREATE TABLE IF NOT EXISTS ileo_cache (
  league_id        INTEGER, gw INTEGER, player_id INTEGER,
  rival_count      INTEGER,
  ileo             REAL,
  my_multiplier    REAL,
  swing_per_point  REAL,
  owned_by         TEXT,
  computed_at      TEXT,
  PRIMARY KEY (league_id, gw, player_id)
);

-- Projections and derived state ---------------------------------------------
CREATE TABLE IF NOT EXISTS xp_projection (
  player_id        INTEGER, gw INTEGER, run_id TEXT,
  fixtures         INTEGER,
  exp_minutes      REAL, p_start REAL, p_60 REAL,
  xp_appearance    REAL, xp_goals REAL, xp_assists REAL,
  xp_clean_sheet   REAL, xp_saves REAL, xp_defcon REAL,
  xp_bonus         REAL, xp_conceded REAL, xp_cards REAL,
  xp_total         REAL, xp_variance REAL,
  p_haul_12        REAL,
  p_floor_5        REAL,
  source           TEXT,
  computed_at      TEXT,
  PRIMARY KEY (player_id, gw, run_id)
);
CREATE INDEX IF NOT EXISTS ix_xp_gw ON xp_projection(gw, xp_total DESC);

CREATE TABLE IF NOT EXISTS variance_decomp (
  player_id        INTEGER, gw INTEGER,
  actual_points    REAL,
  xp_pre           REAL,
  xp_underlying    REAL,
  process_delta    REAL,
  luck_delta       REAL,
  verdict          TEXT,
  evidence         TEXT,
  computed_at      TEXT,
  PRIMARY KEY (player_id, gw)
);
CREATE INDEX IF NOT EXISTS ix_variance_verdict ON variance_decomp(gw, verdict);

CREATE TABLE IF NOT EXISTS price_snapshot (
  player_id          INTEGER, captured_at TEXT,
  now_cost           REAL, selected_by_percent REAL,
  transfers_in_event INTEGER, transfers_out_event INTEGER,
  net_transfers      INTEGER,
  PRIMARY KEY (player_id, captured_at)
);
CREATE INDEX IF NOT EXISTS ix_price_snap_time ON price_snapshot(captured_at);

CREATE TABLE IF NOT EXISTS price_change (
  player_id          INTEGER, changed_at TEXT,
  old_cost           REAL, new_cost REAL, direction INTEGER,
  momentum_at_change REAL,
  PRIMARY KEY (player_id, changed_at)
);

CREATE TABLE IF NOT EXISTS price_prediction (
  player_id          INTEGER PRIMARY KEY,
  momentum           REAL, momentum_rate REAL,
  p_rise             REAL, p_fall REAL,
  hours_since_change REAL,
  model              TEXT,
  computed_at        TEXT
);

-- Solver --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS solver_run (
  run_id           TEXT PRIMARY KEY,
  anchor_gw        INTEGER, horizon INTEGER, profile TEXT,
  candidate_count  INTEGER, variable_count INTEGER, constraint_count INTEGER,
  status           TEXT,
  objective        REAL, mip_gap REAL, wall_seconds REAL,
  relaxations      TEXT,
  ft_start         INTEGER, bank_start REAL,
  created_at       TEXT
);

CREATE TABLE IF NOT EXISTS solver_path (
  run_id           TEXT, path_rank INTEGER,
  profile          TEXT, label TEXT,
  total_xp         REAL, total_hits INTEGER, net_xp REAL,
  end_ft           INTEGER, end_bank REAL, end_team_value REAL,
  path_variance    REAL,
  chip_used        TEXT, chip_gw INTEGER,
  PRIMARY KEY (run_id, path_rank)
);

CREATE TABLE IF NOT EXISTS solver_move (
  run_id           TEXT, path_rank INTEGER, gw INTEGER, move_index INTEGER,
  player_out       INTEGER, player_in INTEGER,
  cost_delta       REAL, xp_delta REAL,
  is_hit           INTEGER,
  rationale        TEXT,
  PRIMARY KEY (run_id, path_rank, gw, move_index)
);

-- The operator's own plan. Local only; never posted to FPL.
CREATE TABLE IF NOT EXISTS planned_move (
  gw               INTEGER, move_index INTEGER,
  player_out       INTEGER, player_in INTEGER,
  source           TEXT,
  note             TEXT, created_at TEXT,
  PRIMARY KEY (gw, move_index)
);

-- Cache and control plane ---------------------------------------------------
CREATE TABLE IF NOT EXISTS cache_entry (
  cache_key        TEXT PRIMARY KEY,
  tier             TEXT NOT NULL,
  payload          BLOB,
  etag             TEXT,
  fetched_at       TEXT NOT NULL,
  soft_expires_at  TEXT,
  hard_expires_at  TEXT,
  frozen           INTEGER DEFAULT 0,
  hits             INTEGER DEFAULT 0,
  bytes            INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cache_tier ON cache_entry(tier, hard_expires_at);

CREATE TABLE IF NOT EXISTS job (
  job_id           TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  args             TEXT,
  state            TEXT NOT NULL,
  priority         INTEGER DEFAULT 5,
  attempts         INTEGER DEFAULT 0,
  max_attempts     INTEGER DEFAULT 3,
  progress         REAL DEFAULT 0.0,
  progress_note    TEXT,
  result           TEXT,
  error            TEXT,
  runner           TEXT,
  heartbeat_at     TEXT,
  enqueued_at      TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_state ON job(state, priority DESC, enqueued_at);

CREATE TABLE IF NOT EXISTS source_health (
  source               TEXT PRIMARY KEY,
  last_success_at      TEXT, last_failure_at TEXT, last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  requests_window      INTEGER DEFAULT 0,
  window_started_at    TEXT,
  p50_ms               REAL, p95_ms REAL,
  quality              TEXT,
  updated_at           TEXT
);

CREATE TABLE IF NOT EXISTS rate_budget (
  host             TEXT PRIMARY KEY,
  tokens           REAL, capacity REAL, refill_per_sec REAL,
  last_refill_at   TEXT,
  total_requests   INTEGER DEFAULT 0,
  total_429        INTEGER DEFAULT 0
);

-- Hot-path indices on v1 tables ---------------------------------------------
CREATE INDEX IF NOT EXISTS ix_player_gw_gw     ON player_gw(gw);
CREATE INDEX IF NOT EXISTS ix_player_gw_player ON player_gw(player_id, gw DESC);
CREATE INDEX IF NOT EXISTS ix_fixtures_event   ON fixtures(event, team_h, team_a);
CREATE INDEX IF NOT EXISTS ix_players_team     ON players(team_id, element_type);
"""
