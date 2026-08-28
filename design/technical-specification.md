# FPL Squad Assistant — Technical Specification

**Status:** Draft v1.0
**Date:** 2026-08-28
**Owner:** Personal project (private)
**Deployment:** Fully local (single-user, own machine)

---

## 1. Purpose & scope

Build an on-device application that helps me manage my Fantasy Premier League (FPL) squad
and make better week-on-week decisions. The system must:

1. Retrieve squad, player, ownership and transfer data from the official **FPL API**.
2. Ingest football **news/chatter** (injuries, illness, missed training, rotation risk)
   from free sources and make it searchable per player.
3. Optionally produce natural-language **insights and availability summaries** using my
   **personal Claude subscription** (Claude VM) — **no cloud API key or paid provider**.
4. Present everything in a **Streamlit** dashboard running on `localhost`.
5. Incur **£0 recurring cost** and run **entirely locally**.

### Out of scope (v1)
- Automated team submission / making transfers on my behalf.
- Multi-user / hosted deployment.
- Mobile-native app (browser access on LAN is a later option).
- Paid data sources (X/Twitter API, premium FFS, etc.).

---

## 2. Constraints & non-functional requirements

| # | Requirement | Detail |
|---|---|---|
| NFR-1 | **Zero recurring cost** | Only free APIs + my existing Claude subscription. |
| NFR-2 | **Local-first** | All data, storage, and UI run on my machine. No servers. |
| NFR-3 | **No models installed on device** | No local LLM/embedding models. AI via Claude subscription only. |
| NFR-4 | **No cloud API keys** | No OpenAI/Gemini/etc. keys required to run core features. |
| NFR-5 | **Offline-capable core** | FPL/news data and keyword search work without any AI. |
| NFR-6 | **Low resource** | Runs comfortably in 8 GB RAM, ~1 GB disk, no GPU. |
| NFR-7 | **Respectful ingestion** | Cache aggressively; ≤1 req/sec to FPL; honour RSS/robots. |
| NFR-8 | **Data privacy** | Only small retrieved snippets are ever handed to Claude. |

---

## 3. Data sources

### 3.1 FPL official API (free, public, no auth)
Base: `https://fantasy.premierleague.com/api/`

| Data | Endpoint | Key fields |
|---|---|---|
| Players, teams, gameweeks, ownership, transfer trends | `bootstrap-static/` | `selected_by_percent`, `form`, `now_cost`, `transfers_in_event`, `transfers_out_event` |
| Player detail & history | `element-summary/{player_id}/` | per-fixture points, minutes |
| Fixtures + difficulty | `fixtures/` | `team_h`, `team_a`, `difficulty` |
| My entry | `entry/{team_id}/` | overall rank, bank, value |
| My picks (per GW) | `entry/{team_id}/event/{gw}/picks/` | 15-man squad, captain |
| Top managers (template) | `leagues-classic/314/standings/` | top-N entry IDs → their picks |
| Live scores | `event/{gw}/live/` | live points |

**Derived signals:** effective ownership among top-N managers, net transfers (price-change
proxy), differentials (low ownership + strong form/fixtures).

### 3.2 News / chatter (free only)
- **RSS**: BBC Sport, Sky Sports football, PhysioRoom (injuries), club official feeds.
- **Reddit**: `r/FantasyPL` via public `.json` endpoints (rate-limited).
- **Excluded**: X/Twitter (paid), premium subscription sites.

---

## 4. System architecture

```
┌──────────────────────────── Your PC (all local) ────────────────────────────┐
│                                                                              │
│  Ingestion (scheduled)          Storage                Presentation          │
│  ┌───────────────────┐          ┌──────────────┐       ┌──────────────────┐  │
│  │ FPL API client    │────────▶ │ SQLite       │──────▶│ Streamlit app    │  │
│  │ RSS/Reddit fetch  │────────▶ │  + FTS5      │──────▶│ (localhost:8501) │  │
│  │ Clean + dedupe    │          │ (news chunks │       │  - Squad board   │  │
│  │ Player tagging    │          │  + FPL data) │       │  - Risk badges   │  │
│  │ (rapidfuzz)       │          └──────────────┘       │  - Transfers     │  │
│  └───────────────────┘                                 │  - News feed     │  │
│                                                        │  - Insights btn  │  │
│                                                        └────────┬─────────┘  │
└─────────────────────────────────────────────────────────────────┼──────────┘
                                                                    │ on-demand
                                                     ┌──────────────▼───────────┐
                                                     │ InsightsProvider (pluggable)│
                                                     │  Claude subscription (VM)  │
                                                     └────────────────────────────┘
```

### 4.1 Components
1. **Ingestion layer** — Python jobs (run manually or via Windows Task Scheduler /
   APScheduler): fetch FPL data, fetch news, clean, dedupe, chunk, tag to players, write
   to SQLite.
2. **Storage layer** — a single **SQLite** database. FPL tables + a `news_chunks` table
   with an **FTS5** virtual table for keyword search. No external DB server.
3. **Retrieval layer** — keyword/BM25 search (FTS5) filtered by `player_id` + recency.
4. **Insights layer (optional, pluggable)** — an `InsightsProvider` interface; the default
   implementation routes to my **Claude subscription** (see §6). Core app works fully
   without it.
5. **Presentation layer** — **Streamlit** dashboard on `localhost:8501`.

---

## 5. Data model (SQLite)

```
teams(id, name, short_name)
players(id, web_name, full_name, team_id, position, now_cost,
        selected_by_percent, form, transfers_in_event, transfers_out_event, status)
fixtures(id, gw, team_h, team_a, kickoff, difficulty_h, difficulty_a)
my_picks(gw, player_id, is_captain, is_vice, multiplier)
top_owned(gw, player_id, top_n_ownership_pct, top_n_captain_pct)   -- derived

news_articles(id, source, url, title, published_at, fetched_at, raw_text)
news_chunks(id, article_id, chunk_index, text, published_at, source, url)
news_chunk_players(chunk_id, player_id, match_score)               -- entity links
news_chunks_fts  -- FTS5(text, content='news_chunks')

insights(id, player_id, signal_type, status, expected_return,
         confidence, summary, source_urls, created_at, provider)   -- from Claude
```

---

## 6. LLM / insights integration — Claude subscription (no API)

Because there is **no cloud API key**, the AI layer is designed as a **pluggable boundary**
so the core app never depends on it. Two providers are specified:

### 6.1 `NullInsightsProvider` (default, £0, offline)
- No LLM. The dashboard shows keyword-matched news and rule-based flags only
  (e.g. FPL `status`/`chance_of_playing` fields, "doubt/knock/illness" keyword hits).
- Guarantees the app is fully usable with zero AI.

### 6.2 `ClaudeSubscriptionProvider` (my Claude VM)
Uses my **personal Claude subscription** rather than a metered API. Supported modes:

- **Mode A — Briefing bundle (manual/agent handoff).**
  1. In the dashboard I select a player (or "whole squad").
  2. The app writes a **briefing bundle** to `briefings/<player>-<date>.md`: the retrieved
     news chunks + a fixed prompt asking for structured JSON
     (`signal_type`, `status`, `expected_return`, `confidence`, `summary`, `sources`).
  3. I run that bundle through Claude on my VM (Claude Code / Claude desktop).
  4. I drop the returned JSON into `exports/`; the app imports it into the `insights` table
     and renders risk badges + summaries.

- **Mode B — Claude Code CLI (if available on the VM).**
  If the `claude` CLI is installed and authenticated on the VM, the app can invoke it
  non-interactively (subprocess) with the same prompt and parse the JSON response
  directly — no manual copy/paste. This is an enhancement, not a requirement.

**Grounding rules:** only the small set of retrieved chunks is sent to Claude (never the
whole DB); Claude must cite source URLs/dates; output is validated against a JSON schema
before storage.

> **Design intent:** the `InsightsProvider` interface makes it trivial to later add a real
> API-based provider without touching the rest of the app.

---

## 7. News chunking & search specification

1. **Fetch** RSS/Reddit → `{source, url, title, body, published_at}`.
2. **Clean** — strip HTML, whitespace; drop boilerplate.
3. **Dedupe** — hash on normalised title + URL; skip near-duplicates.
4. **Chunk** — split article body by paragraph into ~200–400 token chunks with ~50-token
   overlap; keep metadata on every chunk.
5. **Entity tag** — build an alias map from `players` (`web_name`, full name, common
   nicknames). Fuzzy-match chunk text with **`rapidfuzz`** (score threshold, plus team
   context to disambiguate). Write `news_chunk_players`.
6. **Index** — insert into `news_chunks` + `news_chunks_fts` (FTS5).
7. **Retrieve** — query FTS5 with player name/aliases + keyword filters
   (`injury|doubt|knock|illness|training|rotation|suspended`) and a recency window
   (default last 10 days), ranked by BM25 + recency.
8. **(Optional) Summarise** — pass top chunks to the `InsightsProvider` (Claude) for a
   cited summary + structured signal.

**No embeddings in v1** (keeps it model-free and £0). A Tier-2 semantic-search option is
recorded in the design doc as a future extension.

---

## 8. Dashboard features (Streamlit)

- **My Squad** — table of 15 players: ownership %, form, price, next 3 fixtures + FDR,
  and a **risk badge** (from FPL status + news signals).
- **News Feed** — per-player, newest-first, keyword-filterable, links to sources; optional
  Claude summary per player.
- **Transfer Market** — most transferred in/out this GW; differentials; price-rise watch.
- **Template & Differentials** — effective ownership among top-N managers vs my squad.
- **Captaincy Helper** — ranked by fixture difficulty + form + availability risk.

---

## 9. Technology stack

| Concern | Choice | Cost |
|---|---|---|
| Language | Python 3.11+ | £0 |
| HTTP | `httpx` / `requests` | £0 |
| Data | `pandas` | £0 |
| Storage | **SQLite** (+ FTS5, built-in) | £0 |
| Fuzzy entity matching | `rapidfuzz` | £0 |
| RSS parsing | `feedparser` | £0 |
| Scheduling | `APScheduler` or Windows Task Scheduler | £0 |
| UI | **Streamlit** | £0 |
| AI insights | **Personal Claude subscription** (VM) | £0 (already owned) |
| Secrets/config | `python-dotenv` (only for local paths/flags) | £0 |

---

## 10. Infrastructure requirements

- **Machine:** my existing PC (Windows). 8 GB RAM, ~1 GB disk, **no GPU**.
- **Runtime:** local Python virtual environment (`.venv`).
- **Network:** internet for scheduled FPL/news fetches.
- **AI:** my Claude VM + personal Claude subscription for on-demand insights.
- **No** cloud accounts, servers, containers, or API keys.

---

## 11. Cost summary

| Item | Cost |
|---|---|
| FPL API, RSS, Reddit | £0 |
| SQLite, FTS5, Streamlit, Python libs | £0 |
| AI insights (personal Claude subscription) | £0 (already owned) |
| Hosting / infra | £0 (local) |
| **Recurring total** | **£0** |

---

## 12. Security & privacy

- No secrets required for core features; any config lives in a local `.env` (git-ignored).
- Only small retrieved news snippets are handed to Claude — never the full database.
- Personal FPL data and scraped article text stay local and are git-ignored
  (`data/`, `briefings/`, `exports/`).
- Respect source terms: RSS/robots, FPL rate limits, Reddit limits.

---

## 13. Phased delivery

1. **Phase 1 — FPL data + Streamlit board.** FPL client → SQLite; squad, ownership,
   fixtures, transfers. Immediate value, no AI.
2. **Phase 2 — News ingest + FTS5 + tagging.** Per-player searchable news feed, offline, £0.
3. **Phase 3 — Claude insights layer.** `InsightsProvider` + `ClaudeSubscriptionProvider`
   (Mode A first, Mode B if CLI available); risk badges + summaries.
4. **Phase 4 — Decision intelligence.** Template/effective ownership, captaincy helper,
   differentials, price-change watch.
5. **Phase 5 (optional) — Tier-2 semantic search.** Only if keyword search proves limiting;
   would require an embedding route (recorded as a future decision).
