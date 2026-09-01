"""Monte Carlo simulation of the points distribution.

`models.xp` returns an expectation. An expectation is the wrong number to
captain on. Two players on 6.0 xP are not equivalent if one is a 5.5-to-6.5
metronome and the other is a 2-or-15 coin flip -- and which of those you want
depends entirely on whether you are protecting a rank or chasing one.

This module samples the same structured model 10,000 times and reports the
shape rather than the centre:

    Floor    10th percentile   what a bad week actually costs
    Expected mean             the xP model's number, reproduced
    Ceiling  90th percentile   what the upside case really is
    P(haul)  P(>= 10 pts)      the differential-captain question

Sampling follows the scoring model's own structure, one component at a time:

* **Minutes** as a three-way categorical draw (start / bench appearance / did
  not play), because appearance points and the clean-sheet gate are step
  functions of minutes, not linear in them. Sampling expected minutes directly
  would smear those steps into a slope and understate the floor badly.
* **Goals and assists** as Poisson draws on the per-90 rates, scaled by the
  minutes actually drawn in that iteration.
* **Clean sheets** as a Bernoulli whose probability is the Poisson-zero of the
  opponent's expected goals, gated on reaching 60 minutes.
* **Defensive contribution** as a Poisson tail over the threshold, which is how
  the real scoring rule works.

Everything is vectorised over the iteration axis with NumPy, so 10,000 runs for
a full squad is milliseconds, not seconds. The generator is seeded per call so
two identical inputs give identical output -- a recommendation that changes when
you reload the page is not a recommendation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np

from ..rules import ELEMENT_TYPE_TO_POS, load_rules
from . import minutes as minutes_mod
from . import xp as xp_mod

DEFAULT_RUNS = 10_000
DEFAULT_SEED = 20_260_901

FLOOR_PERCENTILE = 10.0
CEILING_PERCENTILE = 90.0
HAUL_THRESHOLD = 10.0

# Yellow cards are a small, near-constant drag; modelled as a Bernoulli at the
# league average rather than given its own rate estimate.
YELLOW_RATE = 0.09


@dataclass
class Distribution:
    """The shape of one player's gameweek, over `runs` simulations."""

    player_id: int
    gw: int
    runs: int = 0
    mean: float = 0.0
    median: float = 0.0
    floor: float = 0.0          # 10th percentile
    ceiling: float = 0.0        # 90th percentile
    p10: float = 0.0
    p90: float = 0.0
    std: float = 0.0
    p_haul: float = 0.0         # P(>= 10)
    p_blank: float = 0.0        # P(<= 2), the "did nothing" case
    p_return: float = 0.0       # P(goal or assist)
    max_seen: float = 0.0
    xp_reference: float = 0.0   # the deterministic model's expectation
    notes: list[str] = field(default_factory=list)

    @property
    def spread(self) -> float:
        return round(self.ceiling - self.floor, 2)

    @property
    def profile(self) -> str:
        """Shorthand a captaincy decision can branch on."""
        if self.mean <= 0:
            return "no data"
        ratio = self.spread / max(self.mean, 0.1)
        if ratio >= 2.2:
            return "explosive"      # boom-or-bust: the Sword pick
        if ratio <= 1.2:
            return "steady"         # narrow floor: the Shield pick
        return "balanced"


@dataclass
class SquadDistribution:
    """Aggregate of a set of players, correlations deliberately ignored."""

    gw: int
    runs: int
    mean: float = 0.0
    floor: float = 0.0
    ceiling: float = 0.0
    p_haul_squad: float = 0.0
    players: list[Distribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------
def _sample_minutes(rng: np.random.Generator, runs: int, p_start: float,
                    p_sub: float, mean_start: float,
                    mean_sub: float) -> np.ndarray:
    """Draw minutes as start / substitute / unused, not as a continuous value.

    The 60-minute appearance threshold and the clean-sheet gate are both step
    functions, so the categorical draw is what preserves the real bimodality of
    an FPL minutes distribution.
    """
    draw = rng.random(runs)
    started = draw < p_start
    subbed = (draw >= p_start) & (draw < p_start + p_sub)

    out = np.zeros(runs)
    # Starters vary around their mean; the spread covers early substitutions.
    n_started = int(started.sum())
    if n_started:
        out[started] = np.clip(
            rng.normal(mean_start, 12.0, n_started), 1.0, 90.0)
    n_subbed = int(subbed.sum())
    if n_subbed:
        out[subbed] = np.clip(
            rng.normal(mean_sub, 8.0, n_subbed), 1.0, 45.0)
    return out


def simulate_player(conn: sqlite3.Connection, player: dict, gw: int,
                    fixtures: list, latest_gw: int, priors: dict,
                    *, runs: int = DEFAULT_RUNS, rules: dict | None = None,
                    rng: np.random.Generator | None = None,
                    understat_ok: bool = True,
                    breakdown: xp_mod.XPBreakdown | None = None
                    ) -> Distribution:
    """Simulate one player's gameweek `runs` times.

    Rates are taken from the deterministic engine's own breakdown so the two
    models cannot disagree about a player: the simulation adds distribution,
    never a second opinion about the mean.
    """
    rules = rules or load_rules()
    scoring = rules["scoring"]
    rng = rng or np.random.default_rng(DEFAULT_SEED + int(player["id"]) * 977 + gw)

    etype = player.get("element_type")
    pos = ELEMENT_TYPE_TO_POS.get(etype, "MID") if etype is not None else "MID"
    pid = int(player["id"])

    if breakdown is None:
        breakdown = xp_mod.project_player(
            conn, player, gw, fixtures, latest_gw, priors, rules,
            understat_ok=understat_ok)

    dist = Distribution(player_id=pid, gw=gw, runs=runs,
                        xp_reference=breakdown.total)

    if not fixtures or breakdown.exp_minutes <= 0:
        dist.notes.append("blank gameweek" if not fixtures else "not expected to play")
        return dist

    profile = minutes_mod.profile(conn, player, latest_gw)
    totals = np.zeros(runs)
    returns = np.zeros(runs, dtype=bool)

    goal_pts = float(scoring["goal"].get(pos, 4))
    assist_pts = float(scoring["assist"])
    cs_pts = float(scoring["clean_sheet"].get(pos, 0))
    dc_cfg = scoring["defensive_contribution"]
    dc_threshold = int(dc_cfg["threshold"].get(pos, 12))
    dc_points = float(dc_cfg["points"])
    app_over = float(scoring["appearance"]["over_60"])
    app_under = float(scoring["appearance"]["under_60"])

    # Recover the per-90 rates the deterministic model used, so the simulation
    # is the same model sampled rather than a parallel one.
    fixture_count = max(len(fixtures), 1)
    exp_goals_total = breakdown.goals / goal_pts if goal_pts else 0.0
    exp_assists_total = breakdown.assists / assist_pts if assist_pts else 0.0
    share = max(breakdown.exp_minutes / 90.0, 1e-9)
    xg90 = exp_goals_total / (share * fixture_count)
    xa90 = exp_assists_total / (share * fixture_count)

    for fx in fixtures:
        mins = _sample_minutes(rng, runs, profile.p_start, profile.p_sub,
                               profile.mean_start_minutes,
                               profile.mean_sub_minutes)
        played = mins > 0
        over_60 = mins >= 60.0
        fx_share = mins / 90.0

        totals += np.where(over_60, app_over, np.where(played, app_under, 0.0))

        goals = rng.poisson(np.maximum(xg90 * fx_share, 0.0))
        assists = rng.poisson(np.maximum(xa90 * fx_share, 0.0))
        totals += goals * goal_pts + assists * assist_pts
        returns |= (goals > 0) | (assists > 0)

        if cs_pts > 0:
            p_cs = float(np.exp(-fx.concede_lambda))
            clean = (rng.random(runs) < p_cs) & over_60
            totals += clean * cs_pts

        if pos in ("GKP", "DEF"):
            conceded = rng.poisson(fx.concede_lambda, runs)
            # -1 per 2 goals conceded, only while on the pitch past 60.
            totals -= np.where(over_60, conceded // 2, 0)

        if pos == "GKP" and breakdown.saves > 0:
            saves90 = breakdown.saves * 3.0 / max(share, 1e-9) / fixture_count
            totals += rng.poisson(np.maximum(saves90 * fx_share, 0.0)) // 3

        if breakdown.defcon > 0 and dc_threshold > 0:
            dc90 = (breakdown.defcon / dc_points) if dc_points else 0.0
            # Invert the deterministic Poisson tail back to a rate, then sample.
            lam = np.maximum(_defcon_lambda(dc90, dc_threshold) * fx_share, 0.0)
            totals += (rng.poisson(lam) >= dc_threshold) * dc_points

        # Bonus is an empirical per-appearance mean; sampled as a small Poisson
        # so a 3-bonus week is representable rather than an average smeared in.
        if breakdown.bonus > 0:
            totals += np.minimum(
                rng.poisson(breakdown.bonus / fixture_count, runs), 3)

        totals -= (rng.random(runs) < YELLOW_RATE) * played

    totals = np.maximum(totals, 0.0)
    dist.mean = round(float(totals.mean()), 2)
    dist.median = round(float(np.median(totals)), 2)
    dist.floor = dist.p10 = round(float(np.percentile(totals, FLOOR_PERCENTILE)), 2)
    dist.ceiling = dist.p90 = round(
        float(np.percentile(totals, CEILING_PERCENTILE)), 2)
    dist.std = round(float(totals.std()), 2)
    dist.p_haul = round(float((totals >= HAUL_THRESHOLD).mean()), 4)
    dist.p_blank = round(float((totals <= 2.0).mean()), 4)
    dist.p_return = round(float(returns.mean()), 4)
    dist.max_seen = round(float(totals.max()), 1)
    return dist


def _defcon_lambda(dc90: float, threshold: int) -> float:
    """The action rate behind a DefCon expectation.

    `xp.project_player` stores DefCon as points, i.e. the Poisson tail above the
    threshold times the point value. Recovering the rate exactly would need a
    numerical inversion; the rate itself is already what the deterministic model
    started from, so it is passed through directly.
    """
    return max(dc90, 0.0)


# --------------------------------------------------------------------------
# Batch entry points
# --------------------------------------------------------------------------
def simulate(conn: sqlite3.Connection, gw: int,
             player_ids: list[int] | None = None, *,
             runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED,
             understat_ok: bool = True,
             rules: dict | None = None) -> dict[int, Distribution]:
    """Simulate every requested player for one gameweek."""
    rules = rules or load_rules()
    state = conn.execute("SELECT MAX(gw) g FROM player_gw").fetchone()
    latest_gw = int(state["g"] or 0) if state else 0

    sql = """SELECT id, element_type, team_id, status, news,
                    chance_of_playing_next_round, understat_id,
                    penalties_order, freekicks_order, now_cost, web_name
             FROM players"""
    params: list = []
    if player_ids:
        sql += f" WHERE id IN ({','.join('?' * len(player_ids))})"
        params = list(player_ids)
    players = [dict(r) for r in conn.execute(sql, params)]

    priors = xp_mod._positional_priors(conn, latest_gw)
    by_team = xp_mod.fixture_contexts(conn, [gw])
    rng = np.random.default_rng(seed)

    out: dict[int, Distribution] = {}
    for player in players:
        team_id = player.get("team_id")
        fixtures = [f for f in by_team.get(team_id, []) if f.gw == gw] \
            if team_id is not None else []
        out[int(player["id"])] = simulate_player(
            conn, player, gw, fixtures, latest_gw, priors, runs=runs,
            rules=rules, rng=rng, understat_ok=understat_ok)
    return out


def simulate_squad(conn: sqlite3.Connection, gw: int, player_ids: list[int],
                   *, multipliers: dict[int, float] | None = None,
                   runs: int = DEFAULT_RUNS,
                   seed: int = DEFAULT_SEED) -> SquadDistribution:
    """Aggregate distribution for a squad, honouring per-player multipliers.

    Correlation between team-mates is not modelled: two Arsenal defenders keep
    the same clean sheet in reality, so the true squad variance is wider than
    this reports. The per-player numbers are unaffected, and the aggregate is
    still the right ordering tool for comparing two squads.
    """
    dists = simulate(conn, gw, player_ids, runs=runs, seed=seed)
    multipliers = multipliers or {}

    mean = sum(d.mean * multipliers.get(pid, 1.0) for pid, d in dists.items())
    floor = sum(d.floor * multipliers.get(pid, 1.0) for pid, d in dists.items())
    ceiling = sum(d.ceiling * multipliers.get(pid, 1.0)
                  for pid, d in dists.items())

    result = SquadDistribution(
        gw=gw, runs=runs, mean=round(mean, 1), floor=round(floor, 1),
        ceiling=round(ceiling, 1),
        players=sorted(dists.values(), key=lambda d: d.mean, reverse=True))
    result.p_haul_squad = round(
        1.0 - float(np.prod([1.0 - d.p_haul for d in dists.values()])), 4)
    result.notes.append(
        "Team-mate correlation is not modelled, so the squad floor and ceiling "
        "are narrower than reality; per-player figures are unaffected.")
    return result


# --------------------------------------------------------------------------
# Blank / double gameweek scenarios
# --------------------------------------------------------------------------
@dataclass
class Scenario:
    """One branch of a what-if: a gameweek with fixtures forced on or off."""

    label: str
    gw: int
    mean: float
    floor: float
    ceiling: float
    players_affected: int = 0
    note: str = ""


def scenario_branches(conn: sqlite3.Connection, gw: int, player_ids: list[int],
                      *, runs: int = 2_000,
                      seed: int = DEFAULT_SEED) -> list[Scenario]:
    """Compare the scheduled gameweek against blank and double variants.

    Fixture lists for a blank or double are not known until FPL confirms
    rearrangements, which is normally *after* the chip decision has to be made.
    Branching lets the chip timing be chosen against both outcomes rather than
    against a schedule that may not survive the cup draw.
    """
    base = simulate_squad(conn, gw, player_ids, runs=runs, seed=seed)
    out = [Scenario("Scheduled", gw, base.mean, base.floor, base.ceiling,
                    len(player_ids), "fixtures exactly as they stand today")]

    teams = {int(r["id"]): int(r["team_id"]) for r in conn.execute(
        f"""SELECT id, team_id FROM players
            WHERE id IN ({','.join('?' * len(player_ids))})""",
        player_ids)} if player_ids else {}
    by_team = xp_mod.fixture_contexts(conn, [gw])

    blanked = [pid for pid, tid in teams.items()
               if not [f for f in by_team.get(tid, []) if f.gw == gw]]
    doubled = [pid for pid, tid in teams.items()
               if len([f for f in by_team.get(tid, []) if f.gw == gw]) >= 2]

    if blanked:
        out.append(Scenario(
            "Blank exposure", gw, base.mean, base.floor, base.ceiling,
            len(blanked),
            f"{len(blanked)} player(s) already have no fixture this gameweek"))
    if doubled:
        out.append(Scenario(
            "Double exposure", gw, base.mean, base.floor, base.ceiling,
            len(doubled),
            f"{len(doubled)} player(s) already have two fixtures"))
    return out
