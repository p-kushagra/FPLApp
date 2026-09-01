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
