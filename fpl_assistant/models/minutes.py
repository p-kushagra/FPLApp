"""Minutes model. Everything else multiplies through this, so it comes first.

A player who does not play scores 0 regardless of how good they are, and the
single largest error in naive FPL projections is treating a rotation risk and a
nailed starter as equivalent because their per-90 rates match.

Start probability is an empirical rate shrunk toward a positional prior by
appearance count -- the same shrinkage idea already used by `planner.PRIOR_*`,
kept consistent so the two engines cannot disagree about who is nailed.
"""
from __future__ import annotations

import datetime as dt
import math
import re
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


# --------------------------------------------------------------------------
# Injury news parsing
#
# FPL states availability in two places that disagree in useful ways:
# `chance_of_playing_next_round` is a clean percentage, and `news` is free text
# carrying the return date and the nature of the problem. The percentage alone
# is not enough, because it says nothing about the *shape* of the return: a
# player at 100% in his first week back after two months out is not a 90-minute
# player, and treating him as one is the single most expensive minutes error a
# projection can make.
# --------------------------------------------------------------------------

# "Expected back 19 Sep", "Suspended until 19 Sep", "Back 3 Jan"
_RETURN_DATE = re.compile(
    r"(?:back|until|return[s]?(?:\s+on)?)\s+(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# A player who has left the club is never coming back into this squad, unlike
# an injury or a suspension. Distinguishing the two stops a departed player
# from being modelled as a returning one.
_DEPARTED = re.compile(
    r"joined|departed|left the club|loan|transferred", re.IGNORECASE)

# Fraction of normal minutes expected in each of the first weeks back from a
# significant absence. A returning player is eased in: a cameo, then an hour,
# then normal service.
RETURN_RAMP = (0.35, 0.65, 0.85)

# An absence shorter than this needs no ramp -- a one-week knock does not cost
# match fitness.
RAMP_MIN_ABSENCE_DAYS = 21


def parse_return_date(news: str | None,
                      today: dt.date | None = None) -> dt.date | None:
    """Extract an expected return date from FPL's news text.

    FPL omits the year, so the year is inferred as the nearest sensible one:
    a date more than three months in the past is read as next year, which is
    what makes "back 3 Jan" resolve correctly when read in December.
    """
    if not news:
        return None
    match = _RETURN_DATE.search(news)
    if match is None:
        return None
    today = today or dt.date.today()
    day, month = int(match.group(1)), _MONTHS[match.group(2).lower()[:3]]
    try:
        candidate = dt.date(today.year, month, day)
    except ValueError:
        return None
    if (today - candidate).days > 90:
        try:
            candidate = dt.date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def has_departed(player: dict) -> bool:
    """True when the player has left the club rather than being unavailable."""
    if (player.get("status") or "a").lower() != "u":
        return False
    return bool(_DEPARTED.search(player.get("news") or ""))


def return_ramp(player: dict, today: dt.date | None = None) -> float:
    """Minutes multiplier for a player recently back from a long absence.

    Returns 1.0 for anyone who is not in a return window, so the caller can
    multiply unconditionally.
    """
    today = today or dt.date.today()
    news_added = player.get("news_added")
    returns_on = parse_return_date(player.get("news"), today)
    if returns_on is None or returns_on > today:
        return 1.0

    # How long were they out? Without `news_added` the absence length is
    # unknown, and guessing it would invent a ramp for a one-match suspension.
    if not news_added:
        return 1.0
    try:
        flagged = dt.date.fromisoformat(str(news_added)[:10])
    except ValueError:
        return 1.0
    if (returns_on - flagged).days < RAMP_MIN_ABSENCE_DAYS:
        return 1.0

    weeks_back = (today - returns_on).days // 7
    if weeks_back < len(RETURN_RAMP):
        return RETURN_RAMP[weeks_back]
    return 1.0


def availability(player: dict, rotation_score: float = 0.0,
                 today: dt.date | None = None) -> float:
    """Gate from FPL status, chance-of-playing, injury news and rotation risk.

    Status codes: a=available, d=doubtful, i=injured, s=suspended,
    u=unavailable, n=not in squad.
    """
    status = (player.get("status") or "a").lower()

    if status in ("i", "s", "u", "n"):
        # A stated return date that has already passed means the flag is stale;
        # FPL is often a day or two late clearing it. A departed player never
        # gets this reprieve.
        if has_departed(player):
            return 0.0
        returns_on = parse_return_date(player.get("news"), today)
        if returns_on is None or returns_on > (today or dt.date.today()):
            return 0.0
        gate = 0.6   # flag is stale but unconfirmed; heavily discounted
    else:
        chance = player.get("chance_of_playing_next_round")
        gate = 1.0 if chance is None else max(0.0, min(1.0, float(chance) / 100.0))
        if status == "d" and chance is None:
            gate = 0.5  # flagged but no percentage published

    gate *= return_ramp(player, today)

    rotation = max(0.0, min(1.0, rotation_score / ROTATION_SCALE))
    return max(0.0, gate * (1.0 - ROTATION_WEIGHT * rotation))


def availability_alerts(conn: sqlite3.Connection, gw: int) -> list[dict]:
    """Squad players whose availability is in doubt for the coming deadline.

    This is the pre-deadline banner: the one screen the manager must not miss,
    ordered by how much of a hole the player would leave if they do not start.
    """
    # `chance_of_playing_next_round` is selected under its own name, not
    # aliased. `availability()` reads that exact key, so aliasing it to
    # `chance` silently hid the percentage: every flagged player collapsed to
    # the generic 0.5 "no percentage published" branch, and a 75% doubt on an
    # otherwise-available player scored 1.0 and never raised an alert at all.
    rows = conn.execute(
        """SELECT p.id, p.web_name, p.status, p.news, p.news_added,
                  p.chance_of_playing_next_round, p.element_type,
                  mp.multiplier, mp.is_captain, t.short_name AS team
           FROM my_picks mp
           JOIN players p ON p.id = mp.player_id
           LEFT JOIN teams t ON t.id = p.team_id
           WHERE mp.gw = ?""", (gw,)).fetchall()

    alerts: list[dict] = []
    for r in rows:
        player = dict(r)
        gate = availability(player)
        if gate >= 1.0:
            continue
        alerts.append({
            "player_id": int(r["id"]),
            "player": r["web_name"],
            "team": r["team"] or "",
            "position": ELEMENT_TYPE_TO_POS.get(r["element_type"], "MID"),
            "status": r["status"],
            "chance": r["chance_of_playing_next_round"],
            "news": (r["news"] or "")[:140],
            "availability": round(gate, 2),
            "returns": parse_return_date(r["news"]),
            "starting": bool(r["multiplier"]),
            "is_captain": bool(r["is_captain"]),
            # FPL publishes chance-of-playing in 25% steps, so the bands are
            # set to those steps rather than to arbitrary cut-offs:
            #   0%      out, or any flag on the captain     -> critical
            #   25-50%  a coin flip or worse                -> high
            #   75%     a genuine doubt worth seeing early  -> doubt
            # A player at 100% never reaches here (filtered above), so every
            # row in this list is something the manager has to decide about.
            "severity": ("critical" if gate <= 0.0 or r["is_captain"]
                         else ("high" if gate <= 0.5 else "doubt")),
        })

    order = {"critical": 0, "high": 1, "doubt": 2}
    alerts.sort(key=lambda a: (order[a["severity"]], -int(a["starting"])))
    return alerts


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
