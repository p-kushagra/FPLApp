---
name: fpl-weekly-decisions
description: >
  Turn the app's computed data into a weekly FPL decision: transfers in/out,
  captaincy, bench order and chip timing. Use when asked what to do this gameweek,
  who to captain, who to transfer, or to review the squad before the deadline.
  Triggers: this gameweek, who should I captain, transfer, bench, wildcard, chip,
  differential, template, deadline.
---

# FPL Weekly Decisions

Convert the dashboard's numbers into one clear recommendation per decision. All
inputs are pre-computed by the app — **read them, don't recalculate them**.

## Available inputs

| Input | Source |
|---|---|
| Ownership %, form, price, expected points | `players` table |
| Next fixtures + difficulty (FDR) | `fixtures` table |
| Elite ownership & captaincy | `top_owned` table (top-N managers) |
| Rotation / congestion risk | `congestion.rotation_risk()` |
| Availability signals from news | `insights` table |
| Price change pressure | `price_change_percent`, net transfers |

## Decision order

Work in this sequence — later decisions depend on earlier ones.

1. **Availability** — remove anyone flagged out, suspended or at tournament risk.
2. **Transfers** — only if it fixes a problem or captures clear value. A transfer
   that gains under ~2 points of expectation is not worth a hit.
3. **Captaincy** — highest expected return, adjusted for rotation risk.
4. **Bench order** — by likelihood of playing, not by talent.
5. **Chips** — only when the fixture swing genuinely justifies it.

## Core principles

- **Never take a -4 hit for a sideways move.** The replacement must be clearly better
  over the next 3–4 gameweeks, not just this one.
- **Effective ownership drives rank, not raw points.** Owning a 60%-owned player who
  blanks costs nothing in rank; missing them is what hurts.
- **Captain the safest ceiling.** Prefer a nailed premium at home over a rotation
  risk with a slightly higher upside.
- **Fixtures matter over runs, not single games.** Judge the next 3–4, not just one.
- **Price changes are the weakest reason to act.** Never ruin a squad chasing 0.1m.
- **Plan around the tournament cliff.** Before AFCON or a World Cup, value the
  players you will still be able to field.

## Differentials

A differential is only worth it when the player is **both** low-owned *and* has a
genuine route to points (nailed minutes, good fixtures, set pieces). Low ownership
alone is not an edge — it is usually a warning.

Rule of thumb: chase differentials when your rank needs it, protect with the
template when you are defending a good rank.

## Output format

Give a short, ordered recommendation. For each decision: the call, then one line of
justification citing the specific numbers.

> **Transfer:** Out X (🔴 out, hamstring, GW7 return) → In Y (£7.1m, 3 green fixtures, 12% owned)
> **Captain:** Z — home to a bottom-six side, 🟢 minimal rotation risk, 42% elite captaincy
> **Bench order:** A, B, C — A has the only guaranteed start

State clearly when the right answer is **do nothing and roll the transfer**. That is
frequently the correct call and is chronically under-recommended.
