# FPL Squad Assistant (Private)

A **local-first**, near-zero-cost application to manage my Fantasy Premier League (FPL)
squad and drive week-on-week decisions. It combines official FPL data with football
"chatter" (injuries, illness, missed training) gathered from free news sources, and
surfaces it all in a **Streamlit** dashboard running on my own machine.

> **Private repository.** This project is personal and not intended for public sharing.

## What it does

- Pulls squad, ownership %, fixtures, form, prices and transfer trends from the free
  **FPL API**.
- Ingests news via **RSS + Reddit** (free), tags each item to the relevant player, and
  makes it searchable with **SQLite FTS5** (keyword search, no models, offline).
- Optionally generates natural-language **insights and injury/availability summaries**
  using my **personal Claude subscription** on my Claude VM — no cloud API key required.
- Presents everything in a local **Streamlit** dashboard (squad board, risk badges,
  transfer market, template/differentials, captaincy helper).

## Cost

**£0 recurring.** Runs entirely on my machine. No cloud provider, no API keys. The only
AI calls go through my existing personal Claude subscription.

## Design

See [design/technical-specification.md](design/technical-specification.md) and
[design/solution-design.md](design/solution-design.md).

## Quick start

**Windows (PowerShell):**
```powershell
.\run.ps1 -Ingest
```
**macOS / Linux:**
```bash
./run.sh --ingest
```
Then open <http://localhost:8501>. On first run, edit `.env` and set `FPL_TEAM_ID`
(the number in your FPL "Points" page URL), then click the refresh buttons on the home page.

Manual setup and CLI ingestion:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # set FPL_TEAM_ID
python -m fpl_assistant.ingest --all
streamlit run app.py
```

## Porting to another device

This device may not be where the app runs. Full, step-by-step instructions for setting it
up on the target machine (including private-repo auth and Claude config) are in
[docs/PORTING.md](docs/PORTING.md).

## Project structure

```
app.py                     Streamlit home / control panel
pages/                     Squad, News, Transfers, Template, Captaincy
fpl_assistant/             Core package
  config.py                Portable config (.env + sources.yaml)
  db.py                    SQLite schema + FTS5 index
  fpl_client.py            FPL API client
  news_fetch.py            RSS + Reddit fetchers
  chunk.py / entity.py     Chunking + player tagging
  pipeline.py              Ingestion orchestration
  search.py                FTS5 keyword search
  analytics.py             Squad, template, differentials, captaincy, price watch
  insights/                Pluggable AI: Null (offline) + Claude subscription
  ingest.py                CLI: python -m fpl_assistant.ingest --all
config/sources.yaml        News feeds (editable)
data/                      SQLite DB (git-ignored, regenerated locally)
```

## Insights via Claude (no API key)

Set `INSIGHTS_PROVIDER=claude` in `.env`. In **bundle** mode the app writes a briefing to
`briefings/`; run it through your Claude subscription, drop the JSON into `exports/`, and
click **Import**. In **cli** mode (if Claude Code is installed) it calls `claude` directly.
See [docs/PORTING.md](docs/PORTING.md#5-using-claude-for-insights-no-api-key).

## Status

Phases 1–4 implemented: FPL data, news ingest + search, Claude insights layer, and
decision analytics. Optional Tier‑2 semantic search remains a future extension.
