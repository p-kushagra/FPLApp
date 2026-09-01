# Change Request — FPL Decision Engine v2

| Field | Value |
|---|---|
| **CR ID** | CR-2026-001 |
| **Version** | 2.0 (supersedes `docs/archive/CHANGE_REQUEST.v1.md`) |
| **Status** | Ready for implementation |
| **Date** | 2026-09-01 |
| **Design authority** | [fpl_design_doc.md](fpl_design_doc.md) — all ADR, model and UX references below point there |
| **Estimated effort** | 58–74 engineering hours across 7 phases |
| **Risk** | Medium. Additive-first; every phase ships behind a feature flag and is independently revertible. |

---

## 1. Executive delta

| Dimension | v1 (current) | v2 (target) |
|---|---|---|
| Product posture | Passive stat dashboard, 10 co-equal pages | Prescriptive decision engine, 2 decision surfaces + demoted lab |
| Gameweek logic | `next_gw()` = `MIN(event) WHERE finished=0` — wrong mid-GW and after postponements | `temporal` state machine driven by `is_current`/`is_next`/`deadline_time` |
| Transfer advice | Sorted table of form/ownership | Rolling-horizon ILP producing 3 rule-legal, budget-feasible paths |
| Free transfers | Not modelled | Full banked-FT recurrence, cap 5, WC/FH retention |
| Ownership | Global top-50k sample only | Global EO **plus** ILEO over a frozen, named rival set |
| Underlying stats | FPL `expected_goals`/`expected_assists` only | Understat xG/xA/xGChain, with FPL as a labelled fallback |
| Concurrency | Fully synchronous; UI blocks on every fetch | `JobRunner` abstraction; fan-out work backgrounded |
| Caching | None — every call hits the network | Tiered stale-while-revalidate with explicit quality states |
| Failure behaviour | Exceptions reach the page | `SourceResult` envelope + degradation matrix; no page raises |
| Price changes | Current net-transfer count only | Time-series snapshots + rise/fall probability model |
| Explainability | Scores without components | Every scalar decomposable; `AssumptionsDrawer` on every surface |

### 1.1 Flagged concerns and the assumptions taken

Raised once, here, so implementation is not blocked on them.

| # | Concern | Decision taken | Where |
|---|---|---|---|
| A1 | The brief specifies Celery/Redis. Requiring a broker plus a worker process to open a local dashboard contradicts the £0/local-first constraint, and Celery has had no official Windows support since 3.1 (the target machine is Windows 11). | Ship a `JobRunner` **protocol**. `LocalThreadRunner` is the default; `CeleryRunner` is implemented and tested but opt-in via `JOB_RUNNER=celery`. Both are delivered — this narrows nothing. | ADR-002 |
| A2 | "Clean frontend/backend architecture" could mean a React+FastAPI rewrite. That is weeks of work on a single-user localhost app and buys nothing the boundary alone provides. | Keep Streamlit; extract all logic into `services/` returning UI-free view-models. A FastAPI adapter over the same view-models remains a ~200-line additive change. **Say the word if a real SPA is wanted — it is a different CR.** | ADR-001 |
| A3 | The ~100 req/min FPL limit is a community-observed figure, not a documented one. | Budget conservatively at **60 req/min** with burst 10, configurable. Treat 429 as a first-class state, not an error. | §5.3 |
| A4 | Understat has no API and no terms permitting bulk scraping at volume. | Cache-first, 1 req / 3 s, identifying User-Agent, hard monthly request cap in config, and a full FPL-native fallback path so the feature is never load-bearing. | ADR-004 |
| A5 | The exact FT accrual during a chip week is stated slightly differently across FPL's rules page and community sources. | Encode as `config/rules.yaml`, defaulting to `chip_accrues_ft: true` (i.e. $f_{t+1} = \min(5, f_t+1)$). **Verify against the live rules page at Phase 0 and flip the flag if needed — no code change required.** | §4.3 |
| A6 | `player_gw` history and `top_owned` are keyed by `gw` but `top_owned` is deleted and rewritten per GW, so historical EO is lost. | Stop deleting. Retain per-GW EO so variance analysis can compare against the template *as it was*. | §4.2 |

---

## 2. Component & API change index

Legend — **N** new · **M** modified · **A** absorbed (logic kept, page shell removed) · **D** deleted · **—** untouched

### 2.1 New modules

| File | Purpose | Public API | Depends on | Phase |
|---|---|---|---|---|
| `fpl_assistant/temporal.py` | GW state machine, deadlines, FT bank | `gw_state(conn) -> GWState`, `anchor_gw(conn) -> int`, `scoring_gw(conn) -> int`, `planning_window(conn, n=5) -> list[int]`, `ft_bank(conn, gw) -> FTBank`, `project_ft(bank, transfers, chip) -> FTBank` | `db`, `config` | 1 |
| `fpl_assistant/sources/__init__.py` | Source package root | re-exports `SourceResult`, `Quality` | — | 2 |
| `fpl_assistant/sources/base.py` | Result envelope + error taxonomy | `@dataclass SourceResult`, `Quality` enum, `SourceError`, `RateLimited`, `Unavailable`, `Malformed` | — | 2 |
| `fpl_assistant/sources/ratelimit.py` | Per-host token bucket | `TokenBucket(rate, burst)`, `acquire(host) -> bool`, `budget_state(host) -> dict` | `db` | 2 |
| `fpl_assistant/sources/fpl.py` | FPL adapter (absorbs `fpl_client.py`) | `bootstrap()`, `fixtures()`, `live(gw)`, `entry(id)`, `picks(id, gw)`, `league_standings(id, page)`, `league_page_iter(id)` — all return `SourceResult` | `ratelimit`, `cache` | 2 |
| `fpl_assistant/sources/understat.py` | Understat scraper | `league_players(season)`, `player(us_id)`, `player_matches(us_id)`, `match(match_id)` | `ratelimit`, `cache` | 3 |
| `fpl_assistant/cache/__init__.py` | SWR façade | `get_or_revalidate(key, tier, fetch_fn) -> SourceResult`, `invalidate(prefix)`, `stats()` | `store`, `jobs` | 2 |
| `fpl_assistant/cache/store.py` | `cache_entry` table access | `read(key)`, `write(key, payload, tier)`, `purge_expired()` | `db` | 2 |
| `fpl_assistant/cache/tiers.py` | TTL tier table | `TIERS: dict[str, Tier]` | — | 2 |
| `fpl_assistant/jobs/__init__.py` | Queue façade | `enqueue(name, **kw) -> job_id`, `status(job_id)`, `pending()`, `reap_stale()` | `runner_*` | 2 |
| `fpl_assistant/jobs/base.py` | `JobRunner` protocol | `class JobRunner(Protocol): submit, status, cancel` | — | 2 |
| `fpl_assistant/jobs/runner_local.py` | Default runner | `LocalThreadRunner` — `ThreadPoolExecutor` + `job` table | `db` | 2 |
| `fpl_assistant/jobs/runner_celery.py` | Optional runner | `CeleryRunner`, `celery_app` | `celery`, `redis` | 7 |
| `fpl_assistant/jobs/tasks.py` | Job catalogue (pure fns) | `refresh_reference`, `ingest_history_gw`, `understat_fanout`, `freeze_rivals`, `poll_live`, `recompute_xp`, `solve_paths` | all sources | 2 |
| `fpl_assistant/resolve/matcher.py` | FPL ↔ Understat resolution | `resolve_all(conn) -> ResolveReport`, `resolve_one(conn, player_id)`, `unresolved(conn)` | `rapidfuzz` | 3 |
| `fpl_assistant/resolve/aliases.py` | Override loader | `load_overrides(cfg)`, `add_override(fpl_id, us_id)` | `config` | 3 |
| `fpl_assistant/models/minutes.py` | Minutes / start model (§7.1) | `start_probability(conn, pid, gw)`, `expected_minutes(conn, pid, fixture)` | `db`, `congestion` | 4 |
| `fpl_assistant/models/xp.py` | Expected points engine (§7.2–7.5) | `project(conn, gws) -> DataFrame`, `project_player(conn, pid, gw) -> XPBreakdown`, `realised_xp(conn, pid, gw)` | `minutes`, `resolve` | 4 |
| `fpl_assistant/models/variance.py` | Luck vs process (§7.9) | `decompose(conn, gw) -> DataFrame`, `buy_candidates(conn, gw)` | `xp` | 4 |
| `fpl_assistant/models/price.py` | Price change model (§7.10) | `snapshot(conn)`, `predict(conn) -> DataFrame`, `calibrate(conn)` | `db` | 6 |
| `fpl_assistant/strategy/eo.py` | EO + ILEO (§7.6) | `global_eo(conn, gw)`, `ileo(conn, gw, rival_ids)`, `swing_matrix(conn, gw, rival_ids)`, `net_swing(conn, gw, rival_ids)` | `db` | 5 |
| `fpl_assistant/strategy/solver.py` | Rolling-horizon ILP (§7.7) | `build_model(ctx, profile) -> LpProblem`, `solve(conn, profile) -> SolverResult`, `three_paths(conn) -> list[SolverResult]`, `candidate_set(conn, k)` | `pulp`, `xp`, `temporal` | 6 |
| `fpl_assistant/strategy/captaincy.py` | Shield/Sword (§7.8) | `matrix(conn, gw, rival_ids) -> DataFrame`, `regime(conn, league_id) -> Regime`, `recommend(conn, gw)` | `xp`, `eo` | 5 |
| `fpl_assistant/strategy/chips.py` | Chip EV timeline | `timeline(conn, horizon) -> list[ChipWindow]`, `chip_ev(conn, chip, gw)` | `planner`, `xp` | 6 |
| `fpl_assistant/services/degrade.py` | Quality envelope for view-models | `@dataclass DataQuality`, `collect(conn) -> DataQuality`, `badge_text(q)` | `cache`, `db` | 2 |
| `fpl_assistant/services/gw_summary.py` | Page 1 view-model | `build(conn, cfg, rival_ids) -> GWSummaryVM` | `eo`, `variance`, `temporal` | 5 |
| `fpl_assistant/services/command_center.py` | Page 2 view-model | `build(conn, cfg, profile) -> CommandCenterVM` | `solver`, `captaincy`, `chips` | 6 |
| `fpl_assistant/ui/components.py` | Shared Streamlit widgets | `temporal_header`, `quality_bar`, `skeleton_table`, `metric_card`, `empty_state`, `assumptions_drawer` | `streamlit` | 5 |
| `pages/0_Gameweek_Summary.py` | **Page 1** | — | `services.gw_summary` | 5 |
| `pages/1_Command_Center.py` | **Page 2** | — | `services.command_center` | 6 |
| `config/rules.yaml` | FPL scoring & transfer rules | — | — | 1 |
| `config/aliases.yaml` | Entity overrides | — | — | 3 |
| `config/leagues.yaml` | Tracked leagues + rival sets | — | — | 5 |
| `config/solver.yaml` | Solver weights & relaxation ladder | — | — | 6 |
| `requirements-async.txt` | Optional Celery stack | — | — | 7 |

### 2.2 Modified modules

| File | Change | Breaking? | Phase |
|---|---|---|---|
| `fpl_assistant/db.py` | Add ~15 tables to `SCHEMA` (§4.2); add `_MIGRATIONS` entries; introduce `schema_version` in `meta` and a versioned migration ladder replacing the ad-hoc column dict. **Keep** `current_gw()` as a thin delegate to `temporal.scoring_gw()` so existing callers do not break. | No | 1 |
| `fpl_assistant/config.py` | Load `rules.yaml`, `aliases.yaml`, `leagues.yaml`, `solver.yaml`; add `job_runner`, `understat_enabled`, `fpl_rate_limit`, `solver_time_limit`, `league_ids`, `rival_ids` fields to `Config` | No | 1 |
| `fpl_assistant/pipeline.py` | Rewrite `ingest_fpl` to persist full `gw_state` (deadline, `is_current`, `is_next`, `finished`, `data_checked`) rather than only `current_gw`; add `ingest_price_snapshot`; `ingest_top_owned` **stops deleting prior gameweeks** (concern A6); add `ingest_understat`, `ingest_mini_league`, `freeze_rival_picks`; route all HTTP through `sources.fpl` | No — signatures preserved | 1–3 |
| `fpl_assistant/planner.py` | `next_gw()` becomes a deprecated delegate to `temporal.anchor_gw()`, emitting a `DeprecationWarning`; `captain_ranking` gains an optional `xp_source="model"` to use `models.xp` instead of the heuristic `W_*` blend; `chip_plan` delegates EV to `strategy.chips` | No | 1, 6 |
| `fpl_assistant/fpl_client.py` | **Reduced to a shim** re-exporting `sources.fpl.FplClient` for one release, with a deprecation warning | No | 2 |
| `fpl_assistant/entity.py` | Unchanged for news tagging. Add `normalise_name()` used by `resolve.matcher` so both paths share one normaliser | No | 3 |
| `fpl_assistant/analytics.py` | `squad_overview` gains `xp_next`, `ileo`, `swing` columns; `differentials` accepts `rival_ids` to rank by ILEO gap rather than global ownership; `template` reads retained multi-GW `top_owned` | No | 5 |
| `fpl_assistant/ingest.py` | New CLI flags: `--understat`, `--league`, `--freeze`, `--xp`, `--solve`, `--resolve`, `--prices`; add `--since-gw`; print job ids for backgrounded work | No | 2–6 |
| `fpl_assistant/ui.py` | `boot()` also reaps stale jobs and returns a `DataQuality` object: `boot() -> tuple[Config, Connection, DataQuality]` | **Yes** — **12 call sites** update the unpack: 10 `pages/*.py`, `Refresh_Config.py`, `smoke_test.py` | 2 |
| `Refresh_Config.py` | Add: source-health panel, job-queue panel, unresolved-entity review queue, rival-set picker, request-budget gauge, cache-stats panel | No | 2–5 |
| `requirements.txt` | Add `pulp>=2.8`, `numpy>=1.26`, `streamlit-sortables>=0.3`, `altair>=5.3` | No | 4–6 |
| `smoke_test.py` | Update the `boot()` unpack; extend coverage to the new modules so it stays a genuine import-and-render smoke check | No | 2 |
| `.env.example` | Document `FPL_LEAGUE_IDS`, `JOB_RUNNER`, `UNDERSTAT_ENABLED`, `FPL_RATE_LIMIT`, `SOLVER_TIME_LIMIT`, `SOLVER_HORIZON` | No | 1 |
| `.claude/CLAUDE.md` | Extend the "what is deterministic" table with the new modules so the AI boundary stays accurate | No | 7 |
| `README.md` | Rewrite "What it does" and "Project structure" for the two-surface IA | No | 7 |

### 2.3 UI page changes

| Page | Action | Destination | Rationale |
|---|---|---|---|
| `pages/1_My_Squad.py` | **M** → renumber `2_My_Squad.py` | — | Add xP, ILEO and swing columns |
| `pages/2_News_Feed.py` | **M** → renumber `3_News_Feed.py` | Lab section | Unchanged in substance |
| `pages/3_Transfer_Market.py` | **A** → **D** | Command Center → Market tab | A price/ownership table is a fragment of the transfer decision, not the decision |
| `pages/4_Template_and_Differentials.py` | **A** → **D** | Page 1 → Template tab | Global template only means something next to your own ownership and your rivals' |
| `pages/5_Captaincy.py` | **A** → **D** | Command Center → Captaincy Matrix | Superseded by Shield/Sword (§7.8); a single ranked list cannot express a rank decision |
| `pages/6_Rotation_and_Congestion.py` | **M** → renumber `4_Rotation_and_Congestion.py` | Lab section | Now also feeds `models.minutes` |
| `pages/7_Squad_Briefing.py` | **M** → renumber `7_Squad_Briefing.py` | Lab section | Unchanged |
| `pages/8_Squad_Intelligence.py` | **M** → renumber `5_Squad_Intelligence.py` | Lab section | Unchanged |
| `pages/9_Role_Arbitrage.py` | **M** → renumber `6_Role_Arbitrage.py` | Lab section | Feeds the DefCon term in §7.3 |
| `pages/10_Fixture_Planner.py` | **A** → **D** | Command Center → Chip Timeline | Chip timing belongs beside the transfer path that routes the squad there |
| `Refresh_Config.py` | **M** → move to `pages/8_Refresh_Config.py`; new entry point is `Home.py` | — | The control panel should not be the landing page of a decision engine |

**Net:** 4 page files deleted, 2 created, 1 relocated. **Zero computation modules deleted** — every absorbed page's logic survives in `analytics.py`, `planner.py` or `congestion.py` and is called from a tab.

### 2.4 Deletions and deprecations

| Item | Disposition | Removal release |
|---|---|---|
| `planner.next_gw()` | Deprecated delegate → `temporal.anchor_gw()` | v2.2 |
| `fpl_assistant/fpl_client.py` | Shim → `sources.fpl` | v2.2 |
| `db.current_gw()` | Deprecated delegate → `temporal.scoring_gw()` | v2.2 |
| `planner.W_EP/W_FORM/W_PPG/...` heuristic weights | Retained as the fallback path when `models.xp` has insufficient history (< 3 GWs). Not deleted — the cold-start case is real. | never |
| `entity._STOP_SURNAMES` | Retained for news tagging only; unused on the resolve path (§6.2) | never |
| 4 page files (§2.3) | Deleted after their tabs pass acceptance in Phase 5/6 | Phase 6 |

---

## 3. Configuration changes

### 3.1 `config/rules.yaml` (new)

```yaml
# FPL rules that change between seasons. Never hard-code these in Python.
# Verify against https://fantasy.premierleague.com/help/rules at the start of each season.
version: "2025-26"
verified_on: "2026-09-01"

transfers:
  free_per_gw: 1
  max_banked: 5              # was 2 before 2024/25
  hit_cost: 4
  chip_retains_ft: true      # WC/FH do not consume the bank
  chip_accrues_ft: true      # concern A5 — flip if the rules page disagrees

squad:
  size: 15
  budget: 100.0
  max_per_club: 3
  quota: {GKP: 2, DEF: 5, MID: 5, FWD: 3}
  formation:
    GKP: [1, 1]
    DEF: [3, 5]
    MID: [2, 5]
    FWD: [1, 3]
  sell_price_profit_share: 0.5   # 50% of profit, rounded down to 0.1

scoring:
  appearance: {under_60: 1, over_60: 2}
  goal: {GKP: 10, DEF: 6, MID: 5, FWD: 4}
  assist: 3
  clean_sheet: {GKP: 4, DEF: 4, MID: 1, FWD: 0}
  goals_conceded_per: {GKP: -1, DEF: -1}   # per 2 conceded
  saves_per_3: 1
  penalty_save: 5
  penalty_miss: -2
  yellow: -1
  red: -3
  own_goal: -2
  defensive_contribution:
    points: 2
    threshold: {DEF: 10, MID: 12, FWD: 12}

chips:
  available: [wildcard, bench_boost, triple_captain, free_hit]
  wildcards_per_season: 2
  second_half_start_gw: 20
```

### 3.2 `config/solver.yaml` (new)

```yaml
horizon: 5
candidates_per_position: 40
time_limit_seconds: 30
mip_gap: 0.01

weights:
  gamma: 0.90          # per-GW discount
  bench: 0.10          # beta
  terminal_ft: 1.5     # mu, points per banked FT at horizon end
  team_value: 0.5      # lambda, points per £1m of TV
  differential: 0.0    # eta, only non-zero on the aggressive path

profiles:
  conservative: {max_hits: 0, gamma: 0.95, terminal_ft: 3.0, differential: 0.0}
  aggressive:   {max_hits: 2, gamma: 0.75, terminal_ft: 0.5, differential: 1.2}
  chip_setup:   {max_hits: 1, gamma: 0.90, terminal_ft: 1.0, differential: 0.0}

# Applied in order when the model is infeasible (design doc §5.3)
relaxation_ladder:
  - drop_chip_shape
  - allow_one_hit
  - horizon_to_3
  - greedy_fallback
```

### 3.3 `config/aliases.yaml` (new)

```yaml
# FPL player id -> Understat player id. Hand-written overrides only.
# The resolver writes candidates here for review; it never edits this file itself.
overrides:
  # - fpl_id: 253
  #   understat_id: "1250"
  #   note: "Son Heung-Min / Son — resolver margin below threshold"

# Understat team name -> FPL team short_name, where they differ.
team_aliases:
  "Manchester City": MCI
  "Manchester United": MUN
  "Newcastle United": NEW
  "Wolverhampton Wanderers": WOL
  "Tottenham": TOT
  "Nottingham Forest": NFO
  "Leicester": LEI
  "Brighton": BHA
  "West Ham": WHU
```

### 3.4 `config/leagues.yaml` (new)

```yaml
tracked:
  # - id: 123456
  #   name: "Work league"
  #   rivals: [entry_id, entry_id]   # empty = auto-select top N by rank
default_rival_count: 8
max_rivals: 20          # caps the per-GW freeze request budget
```

### 3.5 `.env` additions

```bash
FPL_LEAGUE_IDS=              # comma-separated classic league ids
JOB_RUNNER=local             # local | celery
UNDERSTAT_ENABLED=true
FPL_RATE_LIMIT=60            # requests per minute (concern A3)
UNDERSTAT_RATE_LIMIT=20      # requests per minute
SOLVER_TIME_LIMIT=30
SOLVER_HORIZON=5
CELERY_BROKER_URL=redis://localhost:6379/0   # only when JOB_RUNNER=celery
```

---

## 4. Database and cache schema

### 4.1 Migration strategy

v1's `_MIGRATIONS` dict adds columns but cannot express table creation, index creation or data backfill, and has no version stamp. Replace it with an ordered ladder while keeping it working for existing databases.

```python
# fpl_assistant/db.py

SCHEMA_VERSION = 2

# Ordered list of (version, sql_or_callable). Applied in order, each in its own
# transaction, recording progress in meta.schema_version. Idempotent: every
# statement uses IF NOT EXISTS, and every backfill is re-runnable.
MIGRATIONS: list[tuple[int, str | Callable]] = [
    (2, _V2_TABLES),          # DDL below
    (2, _v2_backfill_gw_state),
    (2, _v2_backfill_price_snapshot),
]

def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = int(get_meta(conn, "schema_version", "1") or 1)
    for version, step in MIGRATIONS:
        if version <= current:
            continue
        with conn:                       # one transaction per step
            conn.executescript(step) if isinstance(step, str) else step(conn)
        set_meta(conn, "schema_version", version)
        current = version
    return current
```

**Rules.** No destructive migration in v2 — nothing is dropped or renamed. `init_db()` calls `migrate()` after `executescript(SCHEMA)`. The legacy `_migrate()` column-adder is retained and runs first, so a v1 database upgrades in one open. A pre-migration copy is written to `data/fpl.sqlite.bak.v1` on the first v2 open.

### 4.2 New tables

```sql
-- ─── Temporal state ────────────────────────────────────────────────────────
-- One row per gameweek. The single source of truth for §4 of the design doc.
CREATE TABLE IF NOT EXISTS gw_state (
  gw               INTEGER PRIMARY KEY,
  deadline_time    TEXT,              -- ISO8601 UTC
  is_current       INTEGER DEFAULT 0,
  is_next          INTEGER DEFAULT 0,
  finished         INTEGER DEFAULT 0,
  data_checked     INTEGER DEFAULT 0,
  average_score    INTEGER,
  highest_score    INTEGER,
  most_captained   INTEGER,
  chip_plays       TEXT,              -- JSON: [{chip_name, num_played}]
  transfers_made   INTEGER,
  phase            TEXT,              -- PRE_SEASON|UPCOMING|LIVE|SETTLING
  updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_gw_state_phase ON gw_state(phase);

-- Free-transfer bank, per gameweek. Recurrence in design doc §4.3.
CREATE TABLE IF NOT EXISTS ft_bank (
  gw               INTEGER PRIMARY KEY,
  ft_available     INTEGER NOT NULL,  -- f_t entering the GW
  transfers_made   INTEGER DEFAULT 0, -- T_t
  ft_consumed      INTEGER DEFAULT 0, -- q_t
  hits             INTEGER DEFAULT 0, -- h_t
  chip_active      TEXT,              -- NULL | wildcard | freehit | benchboost | 3xc
  event_transfers_cost INTEGER,       -- as reported by the FPL API, for reconciliation
  derived          INTEGER DEFAULT 0, -- 1 if inferred rather than API-confirmed
  updated_at       TEXT
);

-- Chip ledger. Which chips remain, when each was played.
CREATE TABLE IF NOT EXISTS chip_state (
  chip             TEXT PRIMARY KEY,  -- wildcard1|wildcard2|benchboost|3xc|freehit
  available        INTEGER DEFAULT 1,
  played_gw        INTEGER,
  points_gained    INTEGER,           -- backfilled post-hoc for calibration
  updated_at       TEXT
);

-- ─── Understat + entity resolution ─────────────────────────────────────────
-- Deterministic FPL <-> Understat binding. Design doc §6.
CREATE TABLE IF NOT EXISTS entity_map (
  fpl_player_id    INTEGER PRIMARY KEY,
  understat_id     TEXT,
  understat_name   TEXT,
  understat_team   TEXT,
  confidence       REAL,              -- 0..1
  method           TEXT,              -- manual|exact|token|fuzzy
  status           TEXT DEFAULT 'resolved',  -- resolved|unresolved|conflict
  runner_up_score  REAL,              -- margin check evidence (§6.2)
  source_hash      TEXT,              -- hash of the FPL name fields at resolution
  resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_entity_map_status ON entity_map(status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entity_map_us ON entity_map(understat_id)
  WHERE understat_id IS NOT NULL;      -- one Understat player binds to one FPL player

-- Season aggregates from understat.com/league/EPL/{season}
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

-- Per-match rows from understat.com/player/{id} -> matchesData
CREATE TABLE IF NOT EXISTS understat_player_match (
  understat_id     TEXT, match_id TEXT,
  season           INTEGER, match_date TEXT,
  team_title       TEXT, opponent_title TEXT, is_home INTEGER,
  minutes          INTEGER, position TEXT,
  goals            INTEGER, assists INTEGER, shots INTEGER, key_passes INTEGER,
  xg               REAL, xa REAL, npg INTEGER, npxg REAL,
  xg_chain         REAL, xg_buildup REAL,
  fpl_gw           INTEGER,           -- resolved by date -> fixture join
  fetched_at       TEXT,
  PRIMARY KEY (understat_id, match_id)
);
CREATE INDEX IF NOT EXISTS ix_uspm_gw ON understat_player_match(fpl_gw);

-- Team-level season rates, for the opponent adjustment phi_f (§7.2)
CREATE TABLE IF NOT EXISTS understat_team (
  team_title       TEXT, season INTEGER,
  fpl_team_id      INTEGER,
  games            INTEGER, xg REAL, xga REAL, npxg REAL, npxga REAL,
  deep             INTEGER, deep_allowed INTEGER, ppda REAL, ppda_allowed REAL,
  fetched_at       TEXT,
  PRIMARY KEY (team_title, season)
);

-- ─── Mini-league ───────────────────────────────────────────────────────────
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
  is_rival         INTEGER DEFAULT 0,      -- in the selected rival set
  fetched_at       TEXT,
  PRIMARY KEY (league_id, gw, entry_id)
);
CREATE INDEX IF NOT EXISTS ix_standing_rival ON league_standing(league_id, gw, is_rival);

-- Frozen rival squads. ADR-005: immutable once frozen = 1.
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

-- Precomputed ILEO so Page 1 never computes on render (perf budget §9)
CREATE TABLE IF NOT EXISTS ileo_cache (
  league_id        INTEGER, gw INTEGER, player_id INTEGER,
  rival_count      INTEGER,
  ileo             REAL,              -- design doc §7.6
  my_multiplier    REAL,
  swing_per_point  REAL,              -- my_multiplier - ileo
  owned_by         TEXT,              -- JSON: {entry_id: multiplier}
  computed_at      TEXT,
  PRIMARY KEY (league_id, gw, player_id)
);

-- ─── Projections and derived state ─────────────────────────────────────────
-- One row per player per gameweek per model run. Design doc §7.5.
CREATE TABLE IF NOT EXISTS xp_projection (
  player_id        INTEGER, gw INTEGER, run_id TEXT,
  fixtures         INTEGER,           -- |F_{p,t}|: 0 blank, 2 double
  exp_minutes      REAL, p_start REAL, p_60 REAL,
  xp_appearance    REAL, xp_goals REAL, xp_assists REAL,
  xp_clean_sheet   REAL, xp_saves REAL, xp_defcon REAL,
  xp_bonus         REAL, xp_conceded REAL, xp_cards REAL,
  xp_total         REAL, xp_variance REAL,
  p_haul_12        REAL,              -- P(points >= 12), for Sword (§7.8)
  p_floor_5        REAL,              -- P(points >= 5), for Shield
  source           TEXT,              -- understat|fpl_baseline  (degradation flag)
  computed_at      TEXT,
  PRIMARY KEY (player_id, gw, run_id)
);
CREATE INDEX IF NOT EXISTS ix_xp_gw ON xp_projection(gw, xp_total DESC);

-- Post-hoc luck/process decomposition. Design doc §7.9.
CREATE TABLE IF NOT EXISTS variance_decomp (
  player_id        INTEGER, gw INTEGER,
  actual_points    REAL,
  xp_pre           REAL,              -- forecast before the GW
  xp_underlying    REAL,              -- recomputed on realised xG/xA
  process_delta    REAL,              -- Pi
  luck_delta       REAL,              -- Lambda
  verdict          TEXT,              -- deserved_haul|fortunate|unlucky|poor
  evidence         TEXT,              -- JSON: shots, big chances, xG detail
  computed_at      TEXT,
  PRIMARY KEY (player_id, gw)
);
CREATE INDEX IF NOT EXISTS ix_variance_verdict ON variance_decomp(gw, verdict);

-- Price time series. v1 stores only current values, so momentum is unrecoverable.
CREATE TABLE IF NOT EXISTS price_snapshot (
  player_id        INTEGER, captured_at TEXT,
  now_cost         REAL, selected_by_percent REAL,
  transfers_in_event INTEGER, transfers_out_event INTEGER,
  net_transfers    INTEGER,
  PRIMARY KEY (player_id, captured_at)
);
CREATE INDEX IF NOT EXISTS ix_price_snap_time ON price_snapshot(captured_at);

CREATE TABLE IF NOT EXISTS price_change (
  player_id        INTEGER, changed_at TEXT,
  old_cost         REAL, new_cost REAL, direction INTEGER,  -- +1 rise, -1 fall
  momentum_at_change REAL,            -- m_p, for calibration (§7.10)
  PRIMARY KEY (player_id, changed_at)
);

CREATE TABLE IF NOT EXISTS price_prediction (
  player_id        INTEGER PRIMARY KEY,
  momentum         REAL, momentum_rate REAL,
  p_rise           REAL, p_fall REAL,
  hours_since_change REAL,
  model            TEXT,              -- rule|logistic
  computed_at      TEXT
);

-- ─── Solver ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS solver_run (
  run_id           TEXT PRIMARY KEY,
  anchor_gw        INTEGER, horizon INTEGER, profile TEXT,
  candidate_count  INTEGER, variable_count INTEGER, constraint_count INTEGER,
  status           TEXT,              -- Optimal|Feasible|Infeasible|TimeLimit
  objective        REAL, mip_gap REAL, wall_seconds REAL,
  relaxations      TEXT,              -- JSON list of ladder steps applied
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

-- Operator's manual plan from the drag-and-drop planner (§8.4). Never sent to FPL.
CREATE TABLE IF NOT EXISTS planned_move (
  gw               INTEGER, move_index INTEGER,
  player_out       INTEGER, player_in INTEGER,
  source           TEXT,              -- manual|solver
  note             TEXT, created_at TEXT,
  PRIMARY KEY (gw, move_index)
);

-- ─── Cache and control plane ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cache_entry (
  cache_key        TEXT PRIMARY KEY,
  tier             TEXT NOT NULL,
  payload          BLOB,              -- gzipped JSON
  etag             TEXT,
  fetched_at       TEXT NOT NULL,
  soft_expires_at  TEXT,
  hard_expires_at  TEXT,
  frozen           INTEGER DEFAULT 0, -- ml_picks after deadline: never revalidate
  hits             INTEGER DEFAULT 0,
  bytes            INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cache_tier ON cache_entry(tier, hard_expires_at);

CREATE TABLE IF NOT EXISTS job (
  job_id           TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  args             TEXT,              -- JSON
  state            TEXT NOT NULL,     -- queued|running|done|failed|stale|cancelled
  priority         INTEGER DEFAULT 5,
  attempts         INTEGER DEFAULT 0,
  max_attempts     INTEGER DEFAULT 3,
  progress         REAL DEFAULT 0.0,
  progress_note    TEXT,
  result           TEXT,
  error            TEXT,
  runner           TEXT,              -- local|celery
  heartbeat_at     TEXT,              -- stale reaping (ADR-002 consequence)
  enqueued_at      TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_state ON job(state, priority DESC, enqueued_at);

CREATE TABLE IF NOT EXISTS source_health (
  source           TEXT PRIMARY KEY,  -- fpl|fpl_league|understat|rss:{name}
  last_success_at  TEXT, last_failure_at TEXT, last_error TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  requests_window  INTEGER DEFAULT 0,
  window_started_at TEXT,
  p50_ms           REAL, p95_ms REAL,
  quality          TEXT,              -- ok|degraded|down
  updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS rate_budget (
  host             TEXT PRIMARY KEY,
  tokens           REAL, capacity REAL, refill_per_sec REAL,
  last_refill_at   TEXT,
  total_requests   INTEGER DEFAULT 0,
  total_429        INTEGER DEFAULT 0
);
```

### 4.3 Modified v1 tables

| Table | Change | Migration |
|---|---|---|
| `meta` | Add keys: `schema_version`, `anchor_gw`, `scoring_gw`, `last_deadline_at`, `understat_last_ingest`, `league_last_freeze` | Insert-only |
| `top_owned` | **Stop deleting prior gameweeks** (concern A6). No DDL change; `ingest_top_owned` drops its `DELETE FROM top_owned WHERE gw = ?` and uses `INSERT OR REPLACE` alone | Behavioural |
| `players` | Add `understat_id TEXT` denormalised from `entity_map` for join convenience; add `purchase_price REAL` (from `entry/{id}/picks` → `selling_price`/`purchase_price`) to make (C10) exact | `ALTER TABLE ADD COLUMN` via `_MIGRATIONS` |
| `my_picks` | Add `selling_price REAL`, `purchase_price REAL`, `chip TEXT` | `ALTER TABLE ADD COLUMN` |
| `player_gw` | No DDL change. `defensive_contribution` is already present and becomes load-bearing (§7.3) | — |

### 4.4 Indices, retention and maintenance

```sql
-- Hot paths identified from the perf budgets (design doc §9)
CREATE INDEX IF NOT EXISTS ix_player_gw_gw     ON player_gw(gw);
CREATE INDEX IF NOT EXISTS ix_player_gw_player ON player_gw(player_id, gw DESC);
CREATE INDEX IF NOT EXISTS ix_fixtures_event   ON fixtures(event, team_h, team_a);
CREATE INDEX IF NOT EXISTS ix_players_team     ON players(team_id, element_type);
```

**Retention** (weekly maintenance job):

| Table | Policy |
|---|---|
| `cache_entry` | Delete where `hard_expires_at < now` and `frozen = 0` |
| `job` | Delete `done` older than 7 days; keep `failed` for 30 days |
| `price_snapshot` | Keep 1 row per player per hour beyond 30 days; full granularity within 30 days |
| `xp_projection` | Keep the latest 3 `run_id`s per gameweek |
| `news_articles` / `news_chunks` | Existing v1 behaviour, unchanged |
| `league_rival_pick` | Never deleted — the frozen record is the audit trail |

`PRAGMA optimize` on every close; `VACUUM` in the weekly job when free pages exceed 20%.

---

## 5. Task queue and scraper implementation

### 5.1 Job runner abstraction

```python
# fpl_assistant/jobs/base.py
class JobRunner(Protocol):
    def submit(self, name: str, **kwargs) -> str: ...
    def status(self, job_id: str) -> JobStatus: ...
    def cancel(self, job_id: str) -> bool: ...
    def reap_stale(self, heartbeat_timeout_s: int = 300) -> int: ...
```

**`LocalThreadRunner` (default).** A module-level `ThreadPoolExecutor(max_workers=4)`. Every submission writes a `job` row *before* the future is created, so a crash between the two is visible rather than silent. Running jobs heartbeat into `job.heartbeat_at` every 10 s; `boot()` calls `reap_stale()`, marking orphans `stale` and re-enqueueing those with `attempts < max_attempts`.

Streamlit-specific: the executor is created once via `@st.cache_resource` so it survives script reruns rather than leaking a pool per interaction. This is the single most important implementation detail in this section — getting it wrong produces unbounded thread growth that only appears after ~30 minutes of use.

**`CeleryRunner` (opt-in).** `celery_app` with Redis broker and result backend. Tasks in `jobs/tasks.py` are plain functions registered by both runners, so **the task bodies are identical between backends** — only submission differs. Windows operators are directed to `--pool=solo` in the docs (ADR-002).

### 5.2 Job catalogue

| Job | Trigger | Requests | Priority | Idempotent | Notes |
|---|---|---|---|---|---|
| `refresh_reference` | Manual / daily | 2 | 8 | ✓ | bootstrap + fixtures; drives `gw_state` |
| `snapshot_prices` | Hourly | 0 (reuses bootstrap cache) | 4 | ✓ | Writes `price_snapshot`, detects `price_change` |
| `ingest_history_gw(gw)` | On GW settle | 1 | 7 | ✓ | `event/{gw}/live` |
| `freeze_rivals(league_id, gw)` | Deadline crossing | \|R\| + 1 | 9 | ✓ (no-op once frozen) | ADR-005; highest priority — the window closes |
| `understat_fanout(player_ids)` | After `ingest_history_gw` | ≤ len(ids) | 3 | ✓ | Chunked 25 per job; failures isolated per player |
| `resolve_entities` | After `refresh_reference` | 1 | 6 | ✓ | Understat league page, then match |
| `poll_live(gw)` | Every 60 s while LIVE | 1 | 9 | ✓ | Self-rescheduling while state = LIVE |
| `recompute_xp(gws)` | After history or resolve | 0 | 5 | ✓ | Pure compute; new `run_id` per invocation |
| `solve_paths(profile)` | On xP invalidation or user request | 0 | 5 | ✓ | 30 s cap; writes `solver_run` |
| `maintenance` | Weekly | 0 | 1 | ✓ | Retention + VACUUM + `PRAGMA optimize` |

**Concurrency limits.** `max_workers = 4`, but the rate limiter is the real governor — four threads contending for a 1 req/s Understat bucket serialise naturally. `freeze_rivals` takes a per-`(league, gw)` advisory lock via an `INSERT OR IGNORE` sentinel row so two triggers cannot double-fetch.

### 5.3 Rate limiting

Per-host token bucket, persisted in `rate_budget` so limits survive a restart:

```python
# fpl_assistant/sources/ratelimit.py
@dataclass
class Bucket:
    capacity: float      # burst
    refill_per_sec: float

BUCKETS = {
    "fantasy.premierleague.com": Bucket(capacity=10, refill_per_sec=60/60),   # 60/min
    "understat.com":             Bucket(capacity=3,  refill_per_sec=20/60),   # 20/min
}
```

**Acquisition.** Blocking with a 30 s ceiling. Beyond that the call returns `RateLimited` rather than waiting — a UI thread must never block indefinitely on a bucket.

**429 handling.** Exponential backoff with full jitter: `sleep = random.uniform(0, min(60, 2**attempt))`, max 3 attempts. On the third failure, `source_health.quality = 'degraded'`, the host's bucket is halved for 15 minutes, and all priority ≤ 5 jobs for that host are paused. Honour `Retry-After` when present.

**Budget accounting.** Every call increments `rate_budget.total_requests`; the Refresh Config gauge renders consumption against the per-minute limit, so an operator can see *why* a fan-out is slow.

**Cost model per gameweek cycle** — this is what keeps the app inside the budget:

| Activity | Requests | Frequency |
|---|---|---|
| Reference refresh | 2 | 1–2× daily |
| History ingest | 1 | 1× per GW |
| Rival freeze (12 rivals) | 13 | 1× per GW |
| Live polling | 1 | ×60/hour, ~6 h per GW → ~360 |
| Understat incremental | ~40 changed players | 1× per GW |
| Understat cold start | ~700 | once, ever |
| **Steady-state weekly total** | **≈ 420** | well inside 60/min |

### 5.4 Understat scraper

**Endpoints and payloads.**

| URL | Embedded variables | Use |
|---|---|---|
| `/league/EPL/{season}` | `playersData`, `teamsData`, `datesData` | Season aggregates for all players + team rates. **One request covers the whole league** — always try this before any per-player fetch. |
| `/player/{id}` | `groupsData`, `matchesData`, `shotsData` | Per-match history for a single player |
| `/match/{id}` | `shotsData`, `rostersData`, `match_info` | Shot-level detail for variance evidence |
| `/team/{name}/{season}` | `playersData`, `statisticsData` | Team squad, fallback when league page is unavailable |

**Extraction.** The data is embedded as `var playersData = JSON.parse('\x7B\x22...');`:

```python
_PATTERN = re.compile(
    r"var\s+(?P<name>\w+)\s*=\s*JSON\.parse\(\s*'(?P<payload>.*?)'\s*\)\s*;",
    re.DOTALL,
)

def extract(html: str, variable: str) -> list | dict:
    """Pull one JSON.parse payload out of an Understat page.

    Understat hex-escapes the JSON (\\x22 for a quote), so the payload must be
    unescaped before it parses. Raises Malformed rather than returning partial
    data — a markup change must degrade loudly, not silently produce empty stats.
    """
    for m in _PATTERN.finditer(html):
        if m.group("name") == variable:
            raw = m.group("payload").encode().decode("unicode_escape")
            return json.loads(raw)
    raise Malformed(f"variable {variable!r} not found — page structure changed")
```

**Politeness.** `User-Agent: fpl-squad-assistant/2.0 (personal, local use)`, 20 req/min, at most 2 concurrent, cache-first with a 6 h soft TTL, and a `UNDERSTAT_MONTHLY_CAP` in config that hard-stops the adapter. Finished-match pages are cached forever — they cannot change.

**Failure isolation.** `understat_fanout` processes 25 players per job. One player's failure marks that player `baseline` in `xp_projection.source` and continues; it never fails the batch. Three consecutive *batch* failures flip `source_health` to `down` and the degradation badge appears globally.

**GW mapping.** `understat_player_match.match_date` is joined to `fixtures.kickoff_time` within ±36 h **and** on matching club, to assign `fpl_gw`. Unmatched rows are retained with `fpl_gw = NULL` rather than guessed — a mis-assigned match corrupts a gameweek's variance decomposition.

### 5.5 Mini-league ingestion and deadline freeze

**Standings.** `leagues-classic/{id}/standings/?page_standings={n}` — 50 entries per page. Only fetch pages needed to cover the rival set plus the operator's own position.

**Rival selection.** From `config/leagues.yaml`, or auto-select the top `default_rival_count` by current rank plus anyone within 50 points of the operator. Capped by `max_rivals` to bound the request budget.

**The freeze.** `freeze_rivals` is enqueued when `temporal.gw_state()` transitions `UPCOMING → LIVE`:

```
for each rival in rival_set:
    if exists(league_rival_pick where entry_id=r and gw=g and frozen=1):
        skip                                   # idempotent
    picks = fpl.picks(r, g)                    # rate-limited
    if picks.quality is UNAVAILABLE:
        record partial; retry with backoff     # denominator adjusts (§5.3)
    write rows with frozen=1, frozen_at=now
recompute ileo_cache for (league, gw)
```

Pre-deadline, Page 1 reads GW−1 picks and renders the `PROVISIONAL` state. This is honest: nobody's team is knowable before the deadline.

### 5.6 Failure taxonomy and the degradation contract

```python
# fpl_assistant/sources/base.py
class Quality(str, Enum):
    FRESH = "fresh"              # fetched now, or within soft TTL
    STALE = "stale"              # served from cache, revalidation enqueued
    DEGRADED = "degraded"        # fetch failed, older cached value served
    UNAVAILABLE = "unavailable"  # no data at all; consumer applies its fallback

@dataclass(frozen=True)
class SourceResult:
    data: Any | None
    quality: Quality
    source: str
    fetched_at: datetime | None
    age_seconds: float | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.data is not None
```

**The rule adapters must obey:** no exception escapes a source adapter. Everything is mapped onto `Quality`. Consumers branch on `quality`, never on `try/except`. This is what makes the degradation matrix in design-doc §5.3 enforceable rather than aspirational, and it is the first thing to check in code review of any new adapter.

---

## 6. Solver implementation

### 6.1 Module structure

```
fpl_assistant/strategy/solver.py
├── candidate_set(conn, k)          -> list[int]        prune to ~200 (ADR-003)
├── SolverContext                   dataclass: xp matrix, prices, clubs, FT, bank, chips
├── build_model(ctx, profile)       -> pulp.LpProblem   §7.7 variables + C1..C15
├── _add_squad_constraints          C1, C2, C7
├── _add_lineup_constraints         C3, C4, C5, C6
├── _add_transfer_constraints       C8, C9, C10
├── _add_ft_constraints             C11, C12, C13   ← the banked-FT block (§4.3)
├── _add_chip_constraints           C14 + chip-shape
├── greedy_warm_start(ctx)          -> dict            incumbent for CBC
├── solve(conn, profile)            -> SolverResult
├── three_paths(conn)               -> list[SolverResult]
└── _relax_and_retry(ctx, ladder)   -> SolverResult    the relaxation ladder
```

### 6.2 The free-transfer constraint block

This is the subtlest part of the model and the most likely to be implemented wrong, so it is spelled out. Design-doc §7.7 constraints C11–C13:

```python
def _add_ft_constraints(prob, v, ctx, rules):
    F_MAX = rules["transfers"]["max_banked"]      # 5
    BIG_M = 15

    prob += v.f[0] == ctx.ft_start
    for t in ctx.periods:
        transfers_in = pulp.lpSum(v.b[p][t] for p in ctx.players)

        # C11 — hits are the overflow beyond the bank; a chip suppresses them
        prob += v.h[t] >= transfers_in - v.f[t] - BIG_M * v.u[t]
        prob += v.h[t] <= BIG_M * (1 - v.u[t])

        # C12 — FTs consumed = transfers not paid for by hits, zero under a chip
        prob += v.q[t] == transfers_in - v.h[t]
        prob += v.q[t] <= v.f[t]
        prob += v.q[t] <= BIG_M * (1 - v.u[t])

        # C13 — f[t+1] = min(F_MAX, f[t] - q[t] + 1), linearised with z[t]
        prob += v.f[t + 1] <= F_MAX
        prob += v.f[t + 1] <= v.f[t] - v.q[t] + 1
        prob += v.f[t + 1] >= v.f[t] - v.q[t] + 1 - F_MAX * v.z[t]
        prob += v.f[t + 1] >= F_MAX - F_MAX * (1 - v.z[t])
```

**Why this is correct for chips.** With `u[t] = 1`, C12 forces `q[t] = 0`, so C13 reduces to `f[t+1] = min(F_MAX, f[t] + 1)` — the bank is retained and still accrues. Wildcard FT retention is therefore a *consequence* of the model, not a special case bolted on. This must have a dedicated unit test (§8, T-SOLV-04).

### 6.3 Performance and fallbacks

| Concern | Mitigation |
|---|---|
| Model too large | Candidate pruning to ~200 players (ADR-003). Log `variable_count` in `solver_run` — a jump signals pruning regressed |
| CBC slow to a first incumbent | `greedy_warm_start` supplies one via `prob.solverModel` warm start |
| Timeout | `PULP_CBC_CMD(timeLimit=30, gapRel=0.01, msg=0)`; return the incumbent with its gap |
| Infeasible | Relaxation ladder from `config/solver.yaml`, recording each step in `solver_run.relaxations` |
| Non-determinism between runs | Fixed candidate ordering by `(xp DESC, player_id ASC)`; CBC seeded. Two runs on identical data must produce identical paths, or the audit trail is worthless |
| Cold start (< 3 GWs of history) | `models.xp` falls back to `ep_next` plus the v1 `planner.W_*` heuristic; the solver still runs, and the UI labels projections `low confidence` |

---

## 7. Execution roadmap

Each phase is independently shippable and revertible. **A phase is not done until its exit criteria pass.**

### Phase 0 — Baseline and guardrails · 4 h

| Step | Detail |
|---|---|
| 0.1 | Commit the untracked v1 docs (already archived to `docs/archive/`) — they were never in git |
| 0.2 | Add `pytest`, `pytest-cov`, `ruff`, `mypy` to a new `requirements-dev.txt` |
| 0.3 | Create `tests/` with `conftest.py` providing an in-memory SQLite fixture seeded from a golden `bootstrap-static` snapshot |
| 0.4 | Characterisation tests capturing **current** behaviour of `planner.next_gw`, `captain_ranking`, `analytics.squad_overview` — these are the regression net for Phase 1 |
| 0.5 | `.github/workflows/ci.yml` (or a local `scripts/ci.ps1` if staying off GitHub Actions): ruff → mypy → pytest → coverage gate 60% |
| 0.6 | **Verify concern A5** against the live FPL rules page; set `chip_accrues_ft` accordingly |

**Exit:** CI green on an unchanged codebase. Characterisation tests pass. Coverage baseline recorded.

### Phase 1 — Temporal engine and schema v2 · 8 h

| Step | Detail |
|---|---|
| 1.1 | `db.py`: `SCHEMA_VERSION`, `MIGRATIONS` ladder, `migrate()`, auto-backup on first v2 open |
| 1.2 | Add `gw_state`, `ft_bank`, `chip_state` tables |
| 1.3 | `temporal.py`: state machine, `anchor_gw`, `scoring_gw`, `planning_window`, `ft_bank`, `project_ft` |
| 1.4 | `pipeline.ingest_fpl` persists full event metadata to `gw_state` |
| 1.5 | `planner.next_gw` → deprecated delegate; `db.current_gw` → delegate |
| 1.6 | `config/rules.yaml` + loader in `config.py` |
| 1.7 | Update all 11 pages to source gameweeks from `temporal` |

**Exit criteria** — all must hold:
- Freezing the clock at 5 points in a GW cycle (pre-deadline, post-deadline pre-kickoff, mid-live, post-final-whistle pre-bonus, settled) yields the correct `phase`, `anchor_gw` and `scoring_gw`. **Table-driven test, 5 cases.**
- FT recurrence matches a hand-computed 10-gameweek ledger including a Wildcard week and a 2-hit week.
- Characterisation tests from Phase 0 still pass, or their diffs are explicitly reviewed and accepted as the intended fix.
- Opening a v1 `fpl.sqlite` migrates cleanly and `data/fpl.sqlite.bak.v1` exists.

### Phase 2 — Resilience: cache, jobs, rate limiting · 12 h

| Step | Detail |
|---|---|
| 2.1 | `sources/base.py` — `SourceResult`, `Quality`, error taxonomy |
| 2.2 | `sources/ratelimit.py` + `rate_budget` table |
| 2.3 | `cache/` — `cache_entry`, tiers, `get_or_revalidate` |
| 2.4 | `jobs/` — protocol, `LocalThreadRunner`, `job` table, stale reaping, `@st.cache_resource` executor |
| 2.5 | `sources/fpl.py` — port `FplClient`, wrap every method in `SourceResult`; `fpl_client.py` → shim |
| 2.6 | `services/degrade.py` + `ui/components.py` quality bar |
| 2.7 | `ui.boot()` returns `DataQuality`; update all pages (the one breaking change) |
| 2.8 | Refresh Config: source-health, job-queue and budget panels |

**Exit criteria:**
- Fault-injection suite: forced 429, timeout, 500 and malformed JSON each produce the documented `Quality` and never raise.
- Cache hit ratio > 80% on a second consecutive full page tour.
- 100 concurrent enqueues respect the token bucket — measured request spacing ≥ 1/rate.
- Killing the Streamlit process mid-job leaves a `running` row that is reaped to `stale` and re-enqueued on next boot.
- **No thread leak:** 50 script reruns produce exactly one executor.

### Phase 3 — Understat and entity resolution · 10 h

| Step | Detail |
|---|---|
| 3.1 | `sources/understat.py` — extractor, four endpoint methods, politeness |
| 3.2 | `understat_player`, `understat_player_match`, `understat_team`, `entity_map` tables |
| 3.3 | `resolve/matcher.py` — club-scoped ladder with the margin rule; `resolve/aliases.py` |
| 3.4 | `pipeline.ingest_understat` + `jobs.tasks.understat_fanout` |
| 3.5 | GW mapping by date + club join |
| 3.6 | Refresh Config: unresolved-entity review queue writing to `config/aliases.yaml` |

**Exit criteria:**
- ≥ 95% of players with ≥ 90 minutes played resolve automatically at confidence ≥ 0.88.
- **Zero false bindings** in a hand-audited sample of 50, including all known-hard cases (Son, Rodri, the Silvas, both Jesuses, any January signings).
- Understat disabled → every page renders, xP falls back to FPL, badge appears on affected panels.
- Simulated markup change (renamed variable) raises `Malformed`, marks the source `down`, and does **not** write empty stats.
- Cold-start fan-out of 700 players completes within 45 min without a 429.

### Phase 4 — xP engine · 12 h

| Step | Detail |
|---|---|
| 4.1 | `models/minutes.py` — start probability, expected minutes (§7.1) |
| 4.2 | `models/xp.py` — attacking, defensive, DefCon, bonus, assembly (§7.2–7.5) |
| 4.3 | Variance and same-club covariance |
| 4.4 | `xp_projection` table + `recompute_xp` job |
| 4.5 | `models/variance.py` — luck/process decomposition (§7.9) |
| 4.6 | Backtest harness over completed gameweeks |

**Exit criteria:**
- **Calibration:** across all completed GWs of the current season, mean actual points per xP decile is monotonically increasing, and overall mean(actual) is within 5% of mean(xP).
- **Accuracy:** RMSE beats two baselines — FPL's own `ep_next`, and the v1 `planner` heuristic score.
- Blank GW → xP exactly 0.0. Double GW → xP is the sum of two fixture terms with distinct $\phi_f$. **Explicit tests.**
- Full-universe projection over 5 GWs completes in < 8 s.
- Every `xp_total` equals the sum of its component columns to within 1e-6 (explainability, G5).

### Phase 5 — Page 1: Gameweek Summary + ILEO · 10 h

| Step | Detail |
|---|---|
| 5.1 | `league`, `league_standing`, `league_rival_pick`, `ileo_cache` tables |
| 5.2 | `sources/fpl.py` league methods + `jobs.tasks.freeze_rivals` |
| 5.3 | `strategy/eo.py` — global EO, ILEO, swing matrix, net swing (§7.6) |
| 5.4 | `strategy/captaincy.py` — Shield/Sword indices and regime (§7.8) |
| 5.5 | `services/gw_summary.py` view-model |
| 5.6 | `pages/0_Gameweek_Summary.py` + `config/leagues.yaml` + rival picker |
| 5.7 | Delete `pages/4_Template_and_Differentials.py` (absorbed into the Template tab) |

**Exit criteria:**
- Swing arithmetic verified by hand against a 5-rival worked example (design-doc §8.2 sample).
- Deadline crossing freezes rivals exactly once; a second trigger is a no-op.
- Partial freeze (3 of 8 rivals) renders with an adjusted denominator and a visible caption.
- Page 1 first paint < 400 ms on a warm cache.
- All six state boundaries (§8.2) render correctly under forced conditions.

### Phase 6 — Page 2: Command Center + solver · 14 h

| Step | Detail |
|---|---|
| 6.1 | `pulp` dependency; `solver_run`, `solver_path`, `solver_move`, `planned_move` tables |
| 6.2 | `strategy/solver.py` — candidate set, model builder, C1–C15 |
| 6.3 | Three profiles + relaxation ladder + `config/solver.yaml` |
| 6.4 | `strategy/chips.py` — chip EV timeline |
| 6.5 | `models/price.py` + `price_snapshot`/`price_change`/`price_prediction` |
| 6.6 | `services/command_center.py` view-model |
| 6.7 | `pages/1_Command_Center.py` with 5 tabs |
| 6.8 | Drag-and-drop planner (`streamlit-sortables`) with live validation |
| 6.9 | Delete `pages/3_Transfer_Market.py`, `pages/5_Captaincy.py`, `pages/10_Fixture_Planner.py` |

**Exit criteria:**
- **Every** solved path satisfies all FPL rules — verified by an independent validator that does *not* reuse solver code (T-SOLV-06). Squad size, quotas, club limit, formation, budget ≥ 0, FT arithmetic.
- The FT block reproduces a hand-computed 5-GW ledger including a Wildcard week (T-SOLV-04).
- Cold solve < 30 s; warm read < 600 ms.
- Two runs on identical data produce byte-identical paths (determinism).
- Forced infeasibility walks the relaxation ladder and reports which steps fired.
- Drag-and-drop revalidates in < 100 ms and never blocks an illegal intermediate state, only Apply.

### Phase 7 — Consolidation, docs, optional Celery · 8 h

| Step | Detail |
|---|---|
| 7.1 | Renumber remaining pages; `Home.py` entry point; `Refresh_Config.py` → `pages/8_` |
| 7.2 | `jobs/runner_celery.py` + `requirements-async.txt` + Windows `--pool=solo` note |
| 7.3 | Rewrite `README.md`; update `.claude/CLAUDE.md` deterministic-capability table |
| 7.4 | Update `design/technical-specification.md` and `design/solution-design.md` to point at the v2 design doc |
| 7.5 | Update `docs/PORTING.md` for the new config surface |
| 7.6 | Update `scripts/weekly_refresh.*` to drive the new job catalogue |
| 7.7 | Coverage gate to 75%; full end-to-end rehearsal on a clean clone |

**Exit criteria:**
- Clean clone → `.\run.ps1 -Ingest` → both decision pages render with real data, no manual steps beyond setting `FPL_TEAM_ID`.
- `JOB_RUNNER=celery` passes the same job-catalogue integration suite as `local`.
- Docs contain no reference to a deleted page or a v1-only module.

---

## 8. Test plan

| ID | Type | Target | Assertion |
|---|---|---|---|
| T-TEMP-01 | Unit, table-driven | `temporal.gw_state` | 5 clock positions → correct phase, anchor, scoring |
| T-TEMP-02 | Unit | `temporal.project_ft` | 10-GW ledger with WC and a −8 week matches hand calculation |
| T-TEMP-03 | Property | `project_ft` | $f_t \in [0,5]$ always; never negative; never exceeds cap |
| T-TEMP-04 | Regression | `anchor_gw` | Postponed fixture with a low `event` does not drag planning backwards (the v1 bug) |
| T-CACHE-01 | Unit | `get_or_revalidate` | Each of the 6 SWR states returns the documented `Quality` |
| T-CACHE-02 | Integration | Cache + jobs | Stale read serves immediately **and** enqueues exactly one revalidation |
| T-RATE-01 | Unit | `TokenBucket` | 100 acquisitions spaced ≥ 1/rate |
| T-RATE-02 | Fault injection | FPL adapter | 429 → backoff, then `DEGRADED`, never an exception |
| T-JOB-01 | Integration | `LocalThreadRunner` | Submit → running → done; `job` row consistent at each step |
| T-JOB-02 | Integration | Stale reaping | Orphaned `running` row → `stale` → re-enqueued |
| T-JOB-03 | Regression | Streamlit | 50 reruns → exactly one executor (thread-leak guard) |
| T-US-01 | Unit, golden | `understat.extract` | Fixture HTML → expected dict |
| T-US-02 | Unit | `understat.extract` | Renamed variable → `Malformed`, not empty data |
| T-US-03 | Integration | Degradation | Understat off → every page renders, badges shown, xP source = `fpl_baseline` |
| T-RES-01 | Golden | `resolve.matcher` | 50 hand-audited pairs; **zero** false bindings |
| T-RES-02 | Unit | Margin rule | Two candidates within 6 points → `unresolved`, not a guess |
| T-XP-01 | Calibration | `models.xp` | Decile monotonicity; mean within 5% |
| T-XP-02 | Accuracy | `models.xp` | RMSE beats `ep_next` and the v1 heuristic |
| T-XP-03 | Unit | Blank/double | Blank → 0.0 exactly; double → two-term sum |
| T-XP-04 | Unit | Explainability | Components sum to `xp_total` within 1e-6 |
| T-EO-01 | Unit | `strategy.eo` | Worked 5-rival example matches by hand |
| T-EO-02 | Unit | Partial rivals | Denominator adjusts; result flagged partial |
| T-SOLV-01 | Unit | C1–C7 | Squad/lineup legality on a synthetic pool |
| T-SOLV-02 | Unit | C8–C10 | Continuity and budget across 5 periods |
| T-SOLV-03 | Unit | Sell price | 50%-profit rule on a table of purchase/current pairs |
| T-SOLV-04 | Unit | **C11–C13** | FT ledger incl. Wildcard retention — the highest-risk block |
| T-SOLV-05 | Integration | `three_paths` | Three distinct, feasible paths; aggressive ≥ conservative on gross xP |
| T-SOLV-06 | Property | **Independent validator** | Every returned path passes rule validation written without solver code |
| T-SOLV-07 | Performance | `solve` | < 30 s at K=40; logs variable count |
| T-SOLV-08 | Determinism | `solve` | Identical inputs → identical output, twice |
| T-SOLV-09 | Integration | Relaxation ladder | Forced infeasibility → documented steps, reported |
| T-UI-01 | Smoke | All pages | Every page renders on an empty DB without raising |
| T-UI-02 | Smoke | All pages | Every page renders with every source degraded |
| T-E2E-01 | End-to-end | Clean clone | Fresh install → ingest → both pages populated |

**CI gates.** `ruff check` → `mypy fpl_assistant` → `pytest -q --cov` with the coverage gate (60% at Phase 0, 75% at Phase 7). T-SOLV-06 and T-RES-01 are **blocking** — a false entity binding or an illegal squad are the two failures that silently produce confidently wrong advice, which is worse for this product than no advice.

---

## 9. Rollback and feature flags

Every phase ships behind a flag, defaulting **off** until its exit criteria pass.

| Flag | Default | Guards | Rollback |
|---|---|---|---|
| `FEATURE_TEMPORAL_V2` | on after Ph1 | New GW logic | Delegates revert to v1 bodies; no schema rollback needed (additive) |
| `FEATURE_CACHE` | on after Ph2 | SWR layer | Bypass = direct fetch, v1 behaviour |
| `JOB_RUNNER` | `local` | Runner choice | `sync` value runs jobs inline |
| `UNDERSTAT_ENABLED` | `true` | Understat path | `false` → permanent baseline mode, fully supported |
| `FEATURE_XP_MODEL` | on after Ph4 | xP engine | Off → v1 `planner` heuristic |
| `FEATURE_SOLVER` | on after Ph6 | ILP | Off → greedy ranking, page still renders |
| `FEATURE_ILEO` | on after Ph5 | Page 1 matrix | Off → global EO only |

**Schema rollback.** v2 migrations are purely additive — no drops, no renames, no type changes. A v1 codebase opens a v2 database and ignores the new tables. The `data/fpl.sqlite.bak.v1` snapshot is the belt-and-braces path.

---

## 10. Risk register

| # | Risk | L | I | Mitigation | Owner phase |
|---|---|:-:|:-:|---|---|
| R1 | Understat changes markup or blocks the scraper | H | M | ADR-004 fallback; `Malformed` fails loudly; FPL baseline is a complete substitute | 3 |
| R2 | Silent entity mis-binding corrupts a player's xG | M | **H** | Margin rule (§6.2); `conflict` status; blocking golden test T-RES-01 | 3 |
| R3 | CBC too slow or infeasible at realistic sizes | M | H | Pruning, warm start, time limit, relaxation ladder, greedy fallback | 6 |
| R4 | FPL rate-limits or blocks during rival fan-out | M | M | Token bucket at 60/min (below the observed ~100); freeze is once-per-GW; backoff | 2, 5 |
| R5 | Thread leak from Streamlit reruns | M | H | `@st.cache_resource` executor; blocking test T-JOB-03 | 2 |
| R6 | xP model is worse than FPL's own `ep_next` | L | H | Phase 4 exit criterion is explicitly *beating* it; if it fails, ship `ep_next` as the source and keep the assembly | 4 |
| R7 | Migration corrupts an existing database | L | **H** | Additive-only; auto-backup; idempotent steps; per-step transactions | 1 |
| R8 | Scope creep into a full SPA rewrite | M | M | ADR-001 is explicit; a real SPA is a separate CR | — |
| R9 | FPL changes transfer or scoring rules mid-season | M | M | `config/rules.yaml`; no rule is hard-coded | 1 |
| R10 | Solver output is trusted without understanding | M | M | `AssumptionsDrawer`, `PathInspector`, `solver_run` audit trail; every scalar decomposable (G5) | 6 |

---

## 11. Open questions

Not blocking — each has a working default; revisit with real data.

| # | Question | Default taken | Revisit |
|---|---|---|---|
| Q1 | Are Understat's terms compatible with this request volume? | Conservative limits + monthly cap + cache-first (A4) | Before any volume increase |
| Q2 | Should `top_owned` sample more than 50 managers now that history is retained? | Keep 50; the sample is a top-50k *proxy*, and cost is linear | After Phase 5, on measured EO variance |
| Q3 | Correct values for $\rho_{\text{club}}$ (same-club correlation)? | 0.35 defenders / 0.15 attackers, from general football-modelling practice | Estimate empirically from `player_gw` after Phase 4 |
| Q4 | Is the Sword/Shield 2 pts-per-GW threshold right for a 12-person league? | 2.0, from the approximate weekly SD of a head-to-head lead | Calibrate against the operator's own league history |
| Q5 | Should the solver optimise rank-EV rather than points-EV? | Points-EV with a differential bonus on the aggressive path | After a season of ILEO data; needs a rank→points mapping |
| Q6 | Free Hit inside the rolling chain? | Solved as a separate single-period problem (§7.7) | Only if FH placement proves to interact with the chain in practice |
| Q7 | Keep the Claude insights layer as-is? | Yes — unchanged, still the only AI in the system | Not planned |

---

## Appendix — Change summary by the numbers

| Metric | Count |
|---|---|
| New Python modules | 27 |
| Modified Python modules | 11 |
| Deleted page files | 4 (all absorbed into tabs; zero logic lost) |
| New config files | 4 |
| New database tables | 21 |
| Modified database tables | 5 (additive only) |
| New indices | 12 |
| Test cases specified | 34 |
| Feature flags | 7 |
| Phases | 8 (0–7) |
| Estimated effort | 58–74 hours |
