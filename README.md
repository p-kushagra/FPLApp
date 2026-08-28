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

## Status

Design phase. Implementation to follow the phased plan in the design docs.
