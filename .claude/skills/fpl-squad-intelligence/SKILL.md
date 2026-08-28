---
name: fpl-squad-intelligence
description: >
  Interpret the app's learned signals — predicted starting XI, start probability,
  observed club rotation, individual attack/defence impact share, injury knock-on
  effects, comeback watch, substitute impact, head-to-head and new signings. Use when
  asked who will start, who is a team's key player, what happens if someone is
  injured, who benefits, which comebacks to watch, or how a new signing is settling.
  Triggers: predicted XI, will he start, key player, if injured, who replaces, when
  he returns, comeback, new signing, head to head, impact, minutes.
---

# FPL Squad Intelligence

Interpret signals the app has **already learned** from `player_gw` — the per-gameweek
record of who started, how long they played and what they produced.

## Never recompute these

| Signal | Function | Meaning |
|---|---|---|
| Start probability | `start_probability()` | Recency-weighted likelihood of starting |
| Predicted XI | `predicted_xi()` | Ranked most-likely starters for a club |
| Club rotation | `team_rotation_profile()` | Avg XI changes between gameweeks |
| Impact share | `impact_share()` | Share of team xG / xA / defensive actions |
| Key players | `key_players()` | Dependency ranking within a club |
| Injury knock-on | `absence_effect()` | Who gains/loses when a player is out |
| Comebacks | `comeback_watch()` | Minutes ramping up after blanks |
| Sub impact | `sub_impact()` | Points per 90 as sub vs starter |
| Head-to-head | `head_to_head()` | Record vs a specific opponent |
| New signings | `new_signings()` | Arrivals and their integration curve |

## Confidence is mandatory

Every function returns `confidence` (`none`/`low`/`medium`/`high`) and a `sample`
size. **Always state it.** Early in a season the sample is tiny and the numbers look
authoritative but are not.

- `high` (8+ gameweeks) — state it plainly.
- `medium` (4–7) — state it as a lean, not a fact.
- `low` (1–3) — explicitly caveat; one gameweek is noise, not a pattern.
- `none` — say there is not enough data. Do **not** substitute your own priors.

Never present a one-gameweek sample as a trend.

## Interpreting injury knock-on

`absence_effect()` gives `beneficiaries` (gained minutes when the player was out)
and `displaced` (lost minutes). Read it as:

1. **Who starts instead** — highest `minutes_delta`.
2. **Whether output transfers** — a positive `xgi_delta` means the role's attacking
   threat survives the change; near-zero means the team simply creates less.
3. **What happens on return** — the `displaced` list is who loses out again. That is
   the transfer trap: buying the deputy right before the starter returns.

Where a deputy's `xgi_delta` is high and the starter is weeks away, the deputy is a
genuine short-term pick. Where it is flat, avoid the whole situation.

## Observed rotation beats assumed rotation

`config/managers.yaml` holds a curated `rotation_hint`. `team_rotation_profile()`
holds what the club **actually did**. When they disagree, trust the observed value
and say the prior looks stale.

## Known data limits — never fabricate around these

The free FPL API does not provide, and the app therefore does not know:

- **Domestic cup fixtures** — manually listed in `config/calendar.yaml` under
  `cup_rounds`. If empty, cup congestion is simply not modelled. Say so.
- **Manager and coaching staff** — curated in `config/managers.yaml` only.
- **Tactical/possession data** — only FPL strength ratings and xG/xGC proxies exist.
- **Multi-season head-to-head** — only gameweeks stored in `player_gw` are covered.

If asked about any of these and the config is empty, say the data is not available
and point at the file to fill in. Do not invent formations, possession figures,
pressing intensity or historical cup results.

## Output style

Lead with the call, then the evidence and the confidence:

> **Likely starts (0.86, high confidence, 9 GWs)** — 6-game start streak, minutes
> trending up, club is a settled XI.

> **Not enough data (1 GW)** — cannot judge rotation yet.
