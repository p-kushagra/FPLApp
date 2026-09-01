# FPL Squad Assistant (Private)

A **local-first**, near-zero-cost application to manage my Fantasy Premier League (FPL)
squad and drive week-on-week decisions. It combines official FPL data with football
"chatter" (injuries, illness, missed training) gathered from free news sources, and
surfaces it all in a **Streamlit** dashboard running on my own machine.

## What it does

- Pulls squad, ownership %, fixtures, form, prices and transfer trends from the free
  **FPL API**.
- Ingests news from **15 free RSS feeds** (FPL specialists, national outlets and
  club reporters), tags each item to the relevant player, and makes it searchable with
  **SQLite FTS5** (keyword search, no models, offline).
- Optionally generates natural-language **insights and injury/availability summaries**
  using my **personal Claude subscription** on my Claude VM — no cloud API key required.
- Plans ahead: **blank and double gameweeks** (both confirmed and projected from the
  cup calendar), fixture-difficulty runs, head-to-head records and **chip timing**.
- Presents everything in a local **Streamlit** dashboard (squad board, risk badges,
  transfer market, template/differentials, captaincy helper, fixture planner).

## Cost

**£0 recurring.** Runs entirely on my machine. No cloud provider, no API keys. The only
AI calls go through my existing personal Claude subscription.

## Design

See [design/technical-specification.md](design/technical-specification.md) and
[design/solution-design.md](design/solution-design.md).

## Quick start

### Windows: one-click desktop launcher (recommended)

Run once to put a **FPL Command Center** shortcut on your desktop:

```powershell
python scripts/setup_shortcut.py
```

Double-clicking it starts everything with no console window:

| | |
|---|---|
| Dashboard | <http://localhost:8501> (opens automatically once healthy) |
| Background daemon | live polling, price monitor, pre-deadline freeze |
| Daemon log | `data/daemon.log` (rotating, 2 MB × 5) |
| Stop everything | `stop_fpl.bat` |

The daemon is what makes the Process-vs-Luck analysis work. It arms a one-shot
timer for **one hour before each deadline** and freezes the projection vector
into `pre_gw_projections` — a write-once table, so a missed freeze cannot be
recreated later. Leaving the daemon running through the week is the whole
point; the dashboard is optional.

Scripts, if you prefer running them directly:

```
launch_fpl.bat          start daemon + dashboard, open browser
launch_fpl_silent.vbs   same, with no console window (what the shortcut runs)
stop_fpl.bat            graceful shutdown of both
```

Daemon on its own:

```powershell
python -m fpl_assistant.daemon           # run in the foreground
python -m fpl_assistant.daemon --once    # run every job once and exit
python -m fpl_assistant.daemon --status
python -m fpl_assistant.daemon --stop    # graceful; waits for in-flight writes
```

### Or launch by hand

**Windows (PowerShell):**
```powershell
.\run.ps1 -Ingest
```
**macOS / Linux:**
```bash
./run.sh --ingest
```
Then open <http://localhost:8501>. On first run, edit `.env` and set `FPL_TEAM_ID`
(the number in your FPL "Points" page URL), then click the refresh buttons on the
**Refresh Config** page.

Manual setup and CLI ingestion:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # set FPL_TEAM_ID
python -m fpl_assistant.ingest --all
streamlit run Refresh_Config.py
```

## Mini-leagues and rivals

Nothing to type in. Your leagues are read from your own entry
(`/entry/{FPL_TEAM_ID}/` → `leagues.classic`), so joining a new league needs no
config edit — press **Discover my leagues** on the **Leagues & Rivals** page, or
let the daemon do it on its six-hourly pass.

- **Private leagues are tracked automatically.** FPL also enrols you in Overall,
  a country, a region and a club league; those are listed but left untracked,
  because ILEO over seven million entries is just global ownership with extra
  steps. Toggle any of them on if you want them.
- **Rivals default to the top of the table** — the managers you can still
  overtake — capped by `max_rivals` in `config/leagues.yaml`. Override the set
  per league and it persists across weekly standings refreshes.
- **Rival squads are frozen after each deadline**, because picks are hidden
  until the lock. The daemon polls every 20 minutes and skips anyone already
  captured, so a gameweek is caught whenever the machine happens to be awake.
  A deadline that passes with the daemon down cannot be back-filled.

This is what feeds the ILEO swing matrix, the live rank threat meter, the rival
radar and the Shield/Sword captaincy regime. Without a rival set those panels
have nothing to measure against and say so.

## Understat enrichment

Understat supplies the underlying numbers (npxG, xA, xGChain) that sharpen the
xP model. It is **enrichment, never a hard dependency** — when it is unreachable
the app falls back to FPL's own `expected_goals`/`expected_assists` and shows an
`Understat Offline — Using Baseline Stats` badge.

Check connectivity at any time:

```powershell
python scripts/check_understat.py --matches
python -m fpl_assistant.ingest --understat    # league + resolve + per-match
```

The site does not inline its data in the page HTML any more; it serves JSON from
the endpoints its own front-end calls (`getLeagueData`, `getPlayerData`,
`getMatchData`). Those require the header `X-Requested-With: XMLHttpRequest` and
return a 404 error page without it. There is no bot protection involved — the
User-Agent is irrelevant — so if this breaks again, check the endpoint shape
before reaching for a scraping workaround.

### The Understat link is a cache, and it has been wiped before

Resolution writes bindings to `entity_map` (the source of truth) and denormalises
them onto `players.understat_id`, which the xP model and the shot-map join both
read. Two rules keep those in step:

1. **Never write `players` with `INSERT OR REPLACE`.** SQLite implements REPLACE
   as DELETE + INSERT, so it resets every column the statement does not name —
   including `understat_id` and `purchase_price`, which are ours rather than
   FPL's. A REPLACE here silently unresolved all 626 players on *every* FPL
   refresh: shot maps went empty and the xP model dropped to baseline rates
   while Understat itself was perfectly healthy and reporting success. Use the
   `ON CONFLICT(id) DO UPDATE SET` upsert that is there now.
2. `ingest_fpl` calls `matcher.sync_player_links()` afterwards, which rebuilds
   the cache from `entity_map`. It is pure SQL and needs no network, so it is
   also the repair if the column is ever found empty:

```powershell
python -c "import sqlite3; from fpl_assistant.resolve.matcher import sync_player_links; print(sync_player_links(sqlite3.connect('data/fpl.sqlite')))"
```

Symptoms of a wiped cache: an empty shot map, and a `Baseline stats` badge that
survives a successful Understat ingest. Confirm with
`SELECT COUNT(*) FROM players WHERE understat_id IS NOT NULL` — it should be in
the hundreds, never 0.

## Porting to another device

This device may not be where the app runs. Full, step-by-step instructions for setting it
up on the target machine (including private-repo auth and Claude config) are in
[docs/PORTING.md](docs/PORTING.md).

## Project structure

```
Refresh_Config.py          Streamlit entry point (the Refresh Config page)
pages/                     Squad, News, Transfers, Template, Captaincy, Planner
fpl_assistant/             Core package
  config.py                Portable config (.env + sources.yaml)
  db.py                    SQLite schema + FTS5 index
  fpl_client.py            FPL API client
  news_fetch.py            RSS fetching, source naming and feed health probes
  chunk.py / entity.py     Chunking + player tagging
  pipeline.py              Ingestion orchestration
  search.py                FTS5 keyword search
  analytics.py             Squad, template, differentials, price watch
  planner.py               Blank/double gameweeks, fixture runs, captaincy, chips
  insights/                Pluggable AI: Null (offline) + Claude subscription
  ingest.py                CLI: python -m fpl_assistant.ingest --all
  leagues.py               Mini-league discovery + rival selection
config/sources.yaml        News feeds (editable)
data/                      SQLite DB (git-ignored, regenerated locally)
```

## Charts: shot map conventions

`fpl_assistant/ui/charts.py` holds decisions that look like arbitrary constants
and are not. Each is pinned by a test in `tests/test_ui_and_scheduling.py`
(`TestShotMarkerScale`, `TestShotMapGeometry`); read those before tuning one.

* **Marker area encodes xG against a fixed anchor of 1.0 xG = 26px.** Diameter
  goes as `sqrt(xG)` because Plotly's `marker.size` is a diameter and *area* is
  what the eye compares. The anchor is absolute, never the selected player's own
  maximum — per-figure normalisation would draw a defender's best header the same
  size as a striker's tap-in and quietly destroy the cross-player comparison the
  chart exists for.
* **The figure is drawn in metres, not Understat's 0–1 units.** Those units are
  anisotropic (one x-unit is 105m, one y-unit is 68m), so a 1:1 aspect lock on
  them squashes the pitch by a third. In metres the correct lock is exactly 1:1.
* **`scaleanchor` needs `constrain="domain"`.** Plotly's default is
  `constrain="range"`, which honours an aspect ratio by *widening the range until
  the figure fills its container* — so a requested `range` acts only as a floor
  and the same code renders as a tall strip in a narrow column and a stretched
  landscape in a wide one. `domain` shrinks the plotting area instead.
* **The view is a crop (54m × 35m), and crops are not outlined.** Only real pitch
  lines are drawn; boxing the view would invite reading its edge as a touchline.
  The plot background paints the cropped pitch, so grass with no shots on it
  reads as grass rather than as a broken figure.
* **The frame is fixed for every player.** One hopeful 50-yarder must not rescale
  the goalmouth. Shots outside the crop are counted in the subtitle instead of
  being silently dropped or drawn somewhere they were not taken from.
* **Own goals are excluded.** Understat files them under the scorer at 0.00 xG
  and ~100m from the goal being drawn; counting one adds a goal against no xG,
  which reads on the Goals − xG tile as elite finishing.

Chart changes should be **looked at**, not reasoned about. `kaleido` is in
`requirements-dev.txt` for that:

```python
fig.write_image("check.png", width=780, height=520)   # then open it
```

## Keeping data fresh

Most data refreshes itself. A few facts have **no free machine-readable feed** —
European qualifiers, managers, cup round dates — so they live in editable YAML and
are tracked for staleness in [config/references.yaml](config/references.yaml).

```powershell
.\scripts\weekly_refresh.ps1              # refresh now + report stale configs
.\scripts\weekly_refresh.ps1 -Register    # install as a weekly scheduled task
```
```bash
./scripts/weekly_refresh.sh --install-cron   # Tuesdays 08:00
```

Check staleness at any time with `python -m fpl_assistant.check_sources`, or add
`--prompt` to write a Claude briefing listing exactly what to re-verify and where.
The **Refresh Config** page shows the same status, alongside a health check for every
news feed.

## Insights via Claude (no API key)

Set `INSIGHTS_PROVIDER=claude` in `.env`. In **bundle** mode the app writes a briefing to
`briefings/`; run it through your Claude subscription, drop the JSON into `exports/`, and
click **Import**. In **cli** mode (if Claude Code is installed) it calls `claude` directly.
See [docs/PORTING.md](docs/PORTING.md#5-using-claude-for-insights-no-api-key).

## Fixture planning and chips

The **Fixture Planner** page answers the questions you have to get right weeks early:

- **Which gameweeks break.** Confirmed blanks and doubles come from the fixture list.
  Projected ones come from `config/calendar.yaml`: a gameweek sitting on an FA Cup or
  EFL Cup round is a blank waiting to happen for every club still in that competition,
  and the FPL API will not say so for weeks.
- **Who to captain.** Expected points per match times the number of matches that
  gameweek, so a double gameweek roughly doubles the score and a blank scores zero.
  Head-to-head record against the specific opponent, minutes security and rotation risk
  all adjust it. `form` and `points_per_game` are shrunk toward a league prior by
  appearance count, so one early haul cannot outrank a proven premium.
- **When to play each chip.** Bench Boost targets the peak of your squad's fixture
  count, Free Hit the trough, Triple Captain the best double. With nothing confirmed
  the answer is *hold* rather than a gameweek invented from noise.

## Status

Phases 1–4 implemented: FPL data, news ingest + search, Claude insights layer, and
decision analytics, plus forward fixture/chip planning. Optional Tier‑2 semantic search
remains a future extension.
