"""Minutes model. Everything else multiplies through this, so it comes first.

A player who does not play scores 0 regardless of how good they are, and the
single largest error in naive FPL projections is treating a rotation risk and a
nailed starter as equivalent because their per-90 rates match.

Start probability is an empirical rate shrunk toward a positional prior by
appearance count -- the same shrinkage idea already used by `planner.PRIOR_*`,
kept consistent so the two engines cannot disagree about who is nailed.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from ..rules import ELEMENT_TYPE_TO_POS

# Shrinkage: n0 appearances of evidence to outweigh the prior. Matches
# planner.PRIOR_APPEARANCES so both engines agree on what counts as a sample.
PRIOR_APPEARANCES = 5.0

# Positional priors for "starts, given selected in the squad".
PRIOR_START_RATE = {"GKP": 0.75, "DEF": 0.65, "MID": 0.60, "FWD": 0.60}

RECENCY_HALFLIFE_GW = 6.0   # weight halves every 6 gameweeks
FULL_MATCH = 90.0
SIXTY = 60.0

# How much a rotation score suppresses availability. rotation_risk returns
# 0..~6; normalised to 0..1 before this is applied.
ROTATION_WEIGHT = 0.35
ROTATION_SCALE = 6.0


@dataclass(frozen=True)
class MinutesProfile:
    player_id: int
    p_start: float          # probability of starting
    p_sub: float            # probability of appearing off the bench
    p_60: float             # probability of reaching 60 minutes
    p_appear: float         # probability of any appearance
    exp_minutes: float
    availability: float     # status/news/rotation gate, 0..1
    sample: int             # appearances behind the estimate
    mean_start_minutes: float
    mean_sub_minutes: float

    @property
    def confidence(self) -> str:
        if self.sample >= 8:
            return "high"
        if self.sample >= 3:
            return "medium"
        return "low"


def _recency_weight(gw: int, latest_gw: int) -> float:
    return 2.0 ** (-(latest_gw - gw) / RECENCY_HALFLIFE_GW)


def availability(player: dict, rotation_score: float = 0.0) -> float:
    """Gate from FPL status, chance-of-playing and rotation risk.

    Status codes: a=available, d=doubtful, i=injured, s=suspended,
    u=unavailable, n=not in squad.
    """
    status = (player.get("status") or "a").lower()
    if status in ("i", "s", "u", "n"):
        return 0.0

    chance = player.get("chance_of_playing_next_round")
    gate = 1.0 if chance is None else max(0.0, min(1.0, float(chance) / 100.0))

    if status == "d" and chance is None:
        gate = 0.5  # flagged but no percentage published

    rotation = max(0.0, min(1.0, rotation_score / ROTATION_SCALE))
    return max(0.0, gate * (1.0 - ROTATION_WEIGHT * rotation))


def profile(conn: sqlite3.Connection, player: dict, latest_gw: int,
            rotation_score: float = 0.0,
            lookback: int = 12) -> MinutesProfile:
    """Build a minutes profile from `player_gw` history."""
    pid = int(player["id"])
    rows = conn.execute(
        """SELECT gw, minutes, starts FROM player_gw
           WHERE player_id = ? AND gw <= ? AND gw > ?
           ORDER BY gw DESC""",
        (pid, latest_gw, max(0, latest_gw - lookback)),
    ).fetchall()

    etype = player.get("element_type")
    position = ELEMENT_TYPE_TO_POS.get(etype, "MID") if etype is not None else "MID"
    prior = PRIOR_START_RATE.get(position, 0.6)
    avail = availability(player, rotation_score)

    if not rows:
        # No history: lean entirely on the prior, gated by availability.
        p_start = prior * avail
        return MinutesProfile(
            player_id=pid, p_start=p_start, p_sub=0.15 * avail,
            p_60=p_start * 0.85, p_appear=min(1.0, p_start + 0.15 * avail),
            exp_minutes=p_start * 78.0, availability=avail, sample=0,
            mean_start_minutes=78.0, mean_sub_minutes=18.0,
        )

    w_total = w_starts = 0.0
    start_minutes: list[float] = []
    sub_minutes: list[float] = []
    w_60 = 0.0

    for r in rows:
        w = _recency_weight(r["gw"], latest_gw)
        mins = float(r["minutes"] or 0)
        started = bool(r["starts"])
        w_total += w
        if started:
            w_starts += w
            start_minutes.append(mins)
            if mins >= SIXTY:
                w_60 += w
        elif mins > 0:
            sub_minutes.append(mins)

    sample = len(rows)
    raw_start_rate = (w_starts / w_total) if w_total else prior

    # Empirical-Bayes shrinkage toward the positional prior.
    k = sample / (sample + PRIOR_APPEARANCES)
    start_rate = k * raw_start_rate + (1.0 - k) * prior

    mean_start = sum(start_minutes) / len(start_minutes) if start_minutes else 78.0
    mean_sub = sum(sub_minutes) / len(sub_minutes) if sub_minutes else 18.0
    sub_rate = len(sub_minutes) / sample if sample else 0.15

    p_start = start_rate * avail
    p_sub = (1.0 - start_rate) * sub_rate * avail
    # P(60+ | start), also shrunk -- a keeper who starts always reaches 60.
    p60_given_start = (w_60 / w_starts) if w_starts else 0.85
    p_60 = p_start * (k * p60_given_start + (1.0 - k) * 0.85)

    exp_minutes = p_start * mean_start + p_sub * mean_sub

    return MinutesProfile(
        player_id=pid,
        p_start=round(p_start, 4),
        p_sub=round(p_sub, 4),
        p_60=round(p_60, 4),
        p_appear=round(min(1.0, p_start + p_sub), 4),
        exp_minutes=round(exp_minutes, 2),
        availability=round(avail, 4),
        sample=sample,
        mean_start_minutes=round(mean_start, 1),
        mean_sub_minutes=round(mean_sub, 1),
    )


def poisson_at_least(lam: float, k: int) -> float:
    """P(X >= k) for X ~ Poisson(lam). Used for clean sheets and DefCon."""
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    if k <= 0:
        return 1.0
    cdf = 0.0
    term = math.exp(-lam)
    for i in range(k):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def poisson_pmf(lam: float, k: int) -> float:
    if lam < 0 or k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) if lam > 0 else float(k == 0)
