# FPL Squad Assistant — Claude project instructions

A local-first Fantasy Premier League dashboard. Python computes everything
deterministic; Claude is used **only** to interpret free-text news.

## Token discipline (important)

This project runs on a personal Claude subscription, so treat tokens as a scarce
resource.

- **Never recompute what Python already computed.** Ownership, fixture difficulty,
  congestion scores, effective ownership and price pressure all come from the app.
  Read them; do not derive them.
- **Batch, don't loop.** Use the whole-squad briefing (one request for 15 players)
  rather than one request per player.
- **Results are cached.** The app hashes the evidence set and stores answers in the
  `ai_cache` table. The same news is never analysed twice — do not suggest re-running
  an analysis that has not changed.
- **Skills are pre-written.** The rules live in `.claude/skills/`. Refer to them by
  name; do not restate their contents in a prompt or an answer.
- **Be terse.** Short answers, no preamble, no restating the question or the input.
- **Do not browse.** All evidence is supplied locally.

## What is deterministic (never needs AI)

| Capability | Module |
|---|---|
| FPL data ingest | `fpl_assistant/pipeline.py` |
| Fixture congestion, rotation, tournaments | `fpl_assistant/congestion.py` |
| Squad, differentials, template, captaincy, price watch | `fpl_assistant/analytics.py` |
| News keyword search (SQLite FTS5) | `fpl_assistant/search.py` |
| Player tagging in news | `fpl_assistant/entity.py` |

## What actually needs AI

Only one thing: reading unstructured news text and turning it into a structured
availability signal. That is the `fpl-availability-analyst` skill.

The app works fully without AI — `INSIGHTS_PROVIDER=null` gives rule-based flags.

## Layout

```
app.py, pages/          Streamlit dashboard
fpl_assistant/          Core package (ingest, search, analytics, congestion)
fpl_assistant/insights/ Pluggable AI layer + response cache
config/sources.yaml     News feeds
config/calendar.yaml    Tournaments, international breaks, European competitions
config/regions.yaml     FPL region id -> country + confederation
data/fpl.sqlite         All data (git-ignored)
briefings/  exports/    Claude bundle handoff in/out
```

## Editing conventions

- Seasonal facts (tournament dates, which clubs are in Europe) belong in
  `config/calendar.yaml`, never hard-coded in Python.
- New nationality mappings go in `config/regions.yaml`.
- Keep the AI boundary behind `InsightsProvider` — no direct model calls elsewhere.
