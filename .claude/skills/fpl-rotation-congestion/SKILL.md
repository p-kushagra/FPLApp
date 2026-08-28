---
name: fpl-rotation-congestion
description: >
  Reason about Premier League fixture congestion, international breaks and major
  tournaments (AFCON, Copa America, Nations League, Champions League) and how they
  drive squad rotation. Use when asked why a player might be rested, which of my
  players are at tournament risk, or how a busy fixture run affects selection.
  Triggers: rotation, congestion, fixture pile-up, AFCON, Copa America, Nations
  League, Champions League, international break, back-to-back, midweek.
---

# FPL Rotation & Congestion

Context for interpreting the congestion signals the app computes. **The app does the
maths**; you only interpret and explain. Never recompute fixture counts or scores.

## Where the numbers come from

- `config/calendar.yaml` — international break windows, tournament date ranges with
  affected nations, and which clubs are in which midweek competition.
- `config/regions.yaml` — FPL `region` id → country + confederation, used to decide
  whether a tournament removes a given player.
- `fpl_assistant/congestion.py` — computes matches-in-window, minimum rest gap, and a
  0–10 rotation risk score with human-readable reasons.

If a date or team list looks wrong, the fix is to **edit the YAML**, not to reason
around it. Say so plainly.

## Event impact hierarchy

**Tier 1 — removes the player entirely.** They are unavailable for club matches.
- **AFCON** (Jan–Feb): departure can be ~2 weeks early; return depends on how far
  the nation progresses. Affects Egypt, Senegal, Nigeria, Ivory Coast, Ghana,
  Algeria, Morocco, Cameroon, Mali, DR Congo and other CAF nations.
- **Copa America** (summer): CONMEBOL nations; mainly a pre-season/fatigue effect.
- **World Cup** (summer): affects nearly everyone; drives carry-over fatigue.
- **Nations League finals**: a small group of UEFA nations, short window.

**Tier 2 — extra matches, player stays at the club.** The main in-season rotation driver.
- **Champions League** — highest load; heavy rotation in the surrounding league games.
- **Europa League** — Thursday football is especially punishing before a Sunday match.
- **Conference League** — often rotated heavily in its own right.
- **EFL Cup / FA Cup** — managers rotate most aggressively here.

**Tier 3 — travel and fatigue, no missed club matches.**
- **International breaks** — long-haul travel (South America, Asia, Africa) matters
  far more than a short European trip. Players often return 48 hours before kickoff.

## Interpreting the risk score

| Score | Band | Meaning |
|---|---|---|
| 0 | 🟢 Minimal | Normal one-game week. |
| 1–2.9 | 🟡 Low | Some load; a nailed starter is still very likely to play. |
| 3–5.9 | 🟠 Medium | Real rotation risk; check team news before the deadline. |
| 6+ | 🔴 High | Congestion plus tournament or European load; consider benching. |

## Judgement rules

1. **Nailed starters rotate less.** A player averaging 80+ minutes per start is far
   less likely to be rested than a squad player, even in a congested run.
2. **Position matters.** Goalkeepers and centre-backs rotate least; full-backs and
   wide forwards rotate most.
3. **Cup games absorb rotation.** A midweek cup tie often *protects* a starter's
   league place rather than threatening it.
4. **Trust explicit team news over inference.** A manager quote beats any heuristic.
5. **Flag the tournament cliff early.** For AFCON especially, warn several gameweeks
   ahead — the value decision is made before the player leaves, not after.

## Output style

Be brief and decision-oriented. Lead with the recommendation, then the reason:

> 🔴 Bench — third match in 8 days plus a Thursday Europa League tie; rotated in the
> equivalent fixture last month.

Never pad with generic advice about "monitoring press conferences".
