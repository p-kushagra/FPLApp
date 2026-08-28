---
name: fpl-role-arbitrage
description: >
  Spot and time FPL positional arbitrage — players deployed further forward than
  their listed position, who therefore bank points at a better rate than their role
  deserves. Use when asked about a defender playing as a winger, a mispriced player,
  why someone is outscoring their position, when to buy or sell such a player, or how
  long the value lasts. Triggers: playing out of position, defender as winger,
  positional arbitrage, mispriced, role change, wing-back, when does the window close,
  is he still playing there.
---

# FPL Role Arbitrage

FPL scores by **listed position**, not by where a player actually plays. That gap
is the edge.

| | Goal | Clean sheet | Assist |
|---|---|---|---|
| GKP / DEF | **6** | **4** | 3 |
| MID | 5 | 1 | 3 |
| FWD | 4 | 0 | 3 |

A defender pushed up to wing banks **6 points per goal instead of 5**, stays
eligible for **4-point clean sheets**, and is normally priced as a defender. A
midfielder playing as a striker gains nothing on goals but keeps the 1-point clean
sheet — a much weaker version of the same idea.

## The app computes this; you interpret it

`fpl_assistant/role_arbitrage.py` provides:

| Function | Gives you |
|---|---|
| `position_baselines()` | Median threat, xGI and CBI per 90 for each position |
| `role_profile()` | A player's ratios vs those baselines, and inferred role |
| `points_premium()` | Extra points per 90 from the classification alone |
| `window_risk()` | Whether the role is `open`, `closing` or `closed` |
| `arbitrage_candidates()` | Ranked opportunities |

**Never recompute these ratios.** Read them.

## How detection works, and why both halves matter

A player is flagged `advanced` only when **both** hold:

1. Attacking output (threat or xGI per 90) is **≥ 2.5×** their position's median.
2. Defensive workload (**CBI per 90**) is **≤ 0.7×** their position's median.

The second condition is what makes it trustworthy. A centre-back who scores a
header has high attacking output for one week but still clears his own box, so his
CBI stays high and he is correctly excluded. Requiring low CBI isolates players who
genuinely are not defending any more.

CBI is used rather than tackles because clearances, blocks and interceptions are
positional — they happen defending your own box. Tackles happen all over the pitch,
so including them blurs the exact distinction being measured.

## Timing is the whole trade

These roles almost always exist because someone ahead is injured. `window_risk()`
returns:

- **`open`** — no first-choice attacker missing. The role may be permanent. Best case.
- **`closing`** — a first-choice attacker is out but expected back. Buy for the
  window, plan the exit. Name the player and their status.
- **`closed`** — the first choice is fit and playing again. The edge has gone;
  do not recommend a buy, and consider selling.

Only a club's **top-priced attackers** count as credible returners. A fringe squad
player coming back will not reclaim the role.

## What makes a best-case pick

Rank on these, in order:

1. **Cheap** — a £4.0–4.5 defender doing this is an enabler that funds a premium
   elsewhere. The same output at £6.5 is far less useful.
2. **Low ownership** — the edge is gone once it is template.
3. **Window open, or newly closing** — get in early in the injury spell, not late.
4. **Set-piece duty** — corners, free kicks or especially penalties multiply the
   attacking output. The app reports these flags.
5. **Team keeps clean sheets** — a defender in an advanced role at a good defensive
   side collects on both sides of the pitch. This is the strongest combination
   available in FPL.
6. **Minutes security** — cross-check `start_probability()` from the squad
   intelligence skill. A great role is worthless from the bench.

## Exit discipline

Sell when any of these fire:

- `window_risk()` turns **`closed`**.
- The displaced attacker returns to training in the news feed — that is a gameweek
  or two of warning, and earlier than the stats will show it.
- The player's `defence_ratio` climbs back toward 1.0 across recent gameweeks; he
  has been pulled back into the defensive line.
- Price has risen enough that the value has been realised and ownership is now high.

## Honest limits — say these out loud

- The FPL API **never states where a player lined up**. Everything here is inferred
  from output. Call it an inference, not a fact.
- **One gameweek is not a role.** With a small sample, say so and give the ratios
  rather than a confident verdict. A single attacking game from a defender looks
  identical to a role change until it repeats.
- The premium calculation assumes a ~60/40 split of expected involvement between
  goals and assists and a 30% clean-sheet rate. It is an estimate of scale, not a
  projection.

## Output style

Lead with the trade, then the evidence, then the deadline:

> 🟠 **Buy De Cuyper (£4.6, BHA, listed DEF)** — attacking output 49× the defender
> median while defending at 0.23×, worth about +2.1 pts/90 from the classification
> alone. On corners and free kicks. **Window closing:** Mitoma and Minteh are both
> out; expect the role to end when they return, so treat this as a 3–4 week hold.
