"""Expected points engine.

Structured actuarial model, not a black box: every projection decomposes into
named terms that sum to the total, so any recommendation can be interrogated.

    xP = appearance + goals + assists + clean sheet + saves + DefCon + bonus
         - goals conceded - cards

Attacking rates come from Understat when the player is resolved, and from FPL's
own `expected_goals`/`expected_assists` otherwise. The two paths produce the
same shape, so a consumer never branches on the source -- it reads `source` only
to decide whether the UI owes the operator a badge (design doc 5.3).

Summation over a gameweek's fixtures is what makes blanks and doubles fall out
arithmetically: a blank is an empty sum (exactly 0.0, no special case) and a
double is two terms with different opponent adjustments.
"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field

from ..rules import ELEMENT_TYPE_TO_POS, load_rules
from . import minutes as minutes_mod
from . import priors as priors_mod

# Opponent/venue adjustment exponent. 1.0 would take team strength ratios at
# face value; 0.6 damps them, because FPL's strength numbers are coarse.
ALPHA = 0.6

# Shrinkage: minutes of evidence to outweigh the positional mean rate.
SHRINK_MINUTES = 450.0
RECENCY_HALFLIFE_GW = 6.0

# Set-piece premia, applied to expected goals.
PEN_PREMIUM = 0.25
SET_PIECE_PREMIUM = 0.08

SOURCE_UNDERSTAT = "understat"
SOURCE_FPL = "fpl_baseline"


@dataclass
class XPBreakdown:
    """One player, one gameweek. Components sum to `total`."""

    player_id: int
    gw: int
    fixtures: int = 0
    exp_minutes: float = 0.0
    p_start: float = 0.0
    p_60: float = 0.0
    appearance: float = 0.0
    goals: float = 0.0
    assists: float = 0.0
    clean_sheet: float = 0.0
    saves: float = 0.0
    defcon: float = 0.0
    bonus: float = 0.0
    conceded: float = 0.0
    cards: float = 0.0
    total: float = 0.0
    variance: float = 0.0
    p_haul_12: float = 0.0
    p_floor_5: float = 0.0
    source: str = SOURCE_FPL
    notes: list[str] = field(default_factory=list)

    def recompute_total(self) -> float:
        self.total = round(
            self.appearance + self.goals + self.assists + self.clean_sheet
            + self.saves + self.defcon + self.bonus - self.conceded - self.cards,
            4,
        )
        return self.total

    def components(self) -> dict[str, float]:
        return {
            "appearance": self.appearance, "goals": self.goals,
            "assists": self.assists, "clean_sheet": self.clean_sheet,
            "saves": self.saves, "defcon": self.defcon, "bonus": self.bonus,
            "conceded": -self.conceded, "cards": -self.cards,
        }


# --------------------------------------------------------------------------
# Rate estimation
# --------------------------------------------------------------------------
def _shrink(rate: float, sample_minutes: float, prior: float) -> float:
    k = sample_minutes / (sample_minutes + SHRINK_MINUTES)
    return k * rate + (1.0 - k) * prior


def _understat_rates(conn: sqlite3.Connection, understat_id: str,
                     latest_gw: int) -> tuple[float, float, float] | None:
    """(xG90, xA90, minutes) from resolved Understat per-match rows."""
    rows = conn.execute(
        """SELECT fpl_gw, minutes, npxg, xg, xa FROM understat_player_match
           WHERE understat_id = ? AND fpl_gw IS NOT NULL AND fpl_gw <= ?""",
        (understat_id, latest_gw),
    ).fetchall()
    if not rows:
        return None

    w_xg = w_xa = w_min = 0.0
    for r in rows:
        w = 2.0 ** (-(latest_gw - int(r["fpl_gw"])) / RECENCY_HALFLIFE_GW)
        mins = float(r["minutes"] or 0)
        if mins <= 0:
            continue
        # npxG excludes penalties; the penalty premium is applied separately so
        # a penalty taker who loses the duty does not keep the inflated rate.
        w_xg += w * float(r["npxg"] if r["npxg"] is not None else (r["xg"] or 0.0))
        w_xa += w * float(r["xa"] or 0.0)
        w_min += w * mins

    if w_min <= 0:
        return None
    return (90.0 * w_xg / w_min, 90.0 * w_xa / w_min, w_min)


def _fpl_rates(conn: sqlite3.Connection, player_id: int,
               latest_gw: int) -> tuple[float, float, float]:
    """Fallback rates from FPL's own expected_goals / expected_assists."""
    rows = conn.execute(
        """SELECT gw, minutes, expected_goals, expected_assists FROM player_gw
           WHERE player_id = ? AND gw <= ?""",
        (player_id, latest_gw),
    ).fetchall()

    w_xg = w_xa = w_min = 0.0
    for r in rows:
        mins = float(r["minutes"] or 0)
        if mins <= 0:
            continue
        w = 2.0 ** (-(latest_gw - int(r["gw"])) / RECENCY_HALFLIFE_GW)
        w_xg += w * float(r["expected_goals"] or 0.0)
        w_xa += w * float(r["expected_assists"] or 0.0)
        w_min += w * mins

    if w_min <= 0:
        return (0.0, 0.0, 0.0)
    return (90.0 * w_xg / w_min, 90.0 * w_xa / w_min, w_min)


def _positional_priors(conn: sqlite3.Connection, latest_gw: int) -> dict[str, tuple]:
    """League-average xG90/xA90/DC90 per position, for shrinkage targets."""
    rows = conn.execute(
        """SELECT p.element_type,
                  SUM(g.minutes) AS mins,
                  SUM(g.expected_goals) AS xg,
                  SUM(g.expected_assists) AS xa,
                  SUM(g.defensive_contribution) AS dc
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE g.gw <= ? AND g.minutes > 0
           GROUP BY p.element_type""",
        (latest_gw,),
    ).fetchall()

    priors: dict[str, tuple] = {}
    for r in rows:
        pos = ELEMENT_TYPE_TO_POS.get(r["element_type"], "MID")
        mins = float(r["mins"] or 0) or 1.0
        priors[pos] = (
            90.0 * float(r["xg"] or 0) / mins,
            90.0 * float(r["xa"] or 0) / mins,
            90.0 * float(r["dc"] or 0) / mins,
        )
    for pos, default in (("GKP", (0.0, 0.01, 0.0)), ("DEF", (0.04, 0.05, 9.0)),
                         ("MID", (0.12, 0.12, 7.0)), ("FWD", (0.30, 0.12, 3.0))):
        priors.setdefault(pos, default)
    return priors


def _defcon_rate(conn: sqlite3.Connection, player_id: int, latest_gw: int) -> float:
    """Defensive contribution actions per 90."""
    row = conn.execute(
        """SELECT SUM(minutes) mins, SUM(defensive_contribution) dc
           FROM player_gw WHERE player_id = ? AND gw <= ? AND minutes > 0""",
        (player_id, latest_gw),
    ).fetchone()
    mins = float(row["mins"] or 0) if row else 0.0
    if mins <= 0:
        return 0.0
    return 90.0 * float(row["dc"] or 0) / mins


def _saves_rate(conn: sqlite3.Connection, player_id: int, latest_gw: int) -> float:
    row = conn.execute(
        """SELECT SUM(minutes) mins, SUM(saves) sv FROM player_gw
           WHERE player_id = ? AND gw <= ? AND minutes > 0""",
        (player_id, latest_gw),
    ).fetchone()
    mins = float(row["mins"] or 0) if row else 0.0
    return 90.0 * float(row["sv"] or 0) / mins if mins > 0 else 0.0


def _bonus_rate(conn: sqlite3.Connection, player_id: int, latest_gw: int) -> float:
    """Empirical bonus per appearance.

    Modelling BPS rank properly needs all 22 players in a match; this reads the
    realised frequency instead. It ignores within-match correlation, which is an
    acceptable trade for a term worth about half a point.
    """
    row = conn.execute(
        """SELECT COUNT(*) n, SUM(bonus) b FROM player_gw
           WHERE player_id = ? AND gw <= ? AND minutes > 0""",
        (player_id, latest_gw),
    ).fetchone()
    n = int(row["n"] or 0) if row else 0
    if n == 0:
        return 0.0
    return float(row["b"] or 0) / n


# --------------------------------------------------------------------------
# Fixture context
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FixtureCtx:
    fixture_id: int
    gw: int
    team_id: int
    opponent_id: int
    is_home: bool
    difficulty: int
    attack_adj: float
    concede_lambda: float


def _team_strengths(conn: sqlite3.Connection) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams")}


def fixture_contexts(conn: sqlite3.Connection, gws: list[int]) -> dict[int, list[FixtureCtx]]:
    """Per-team fixture list for the window, with strength adjustments applied."""
    teams = _team_strengths(conn)
    if not teams:
        return {}

    def _avg(key: str) -> float:
        vals = [float(t.get(key) or 0) for t in teams.values() if t.get(key)]
        return sum(vals) / len(vals) if vals else 1.0

    avg_att, avg_def = _avg("strength_attack_home"), _avg("strength_defence_home")

    out: dict[int, list[FixtureCtx]] = {tid: [] for tid in teams}
    rows = conn.execute(
        f"""SELECT id, event, team_h, team_a, team_h_difficulty, team_a_difficulty
            FROM fixtures
            WHERE event IN ({','.join('?' * len(gws))})""",
        gws,
    ).fetchall()

    for r in rows:
        for team_id, opp_id, home in ((r["team_h"], r["team_a"], True),
                                      (r["team_a"], r["team_h"], False)):
            team, opp = teams.get(team_id), teams.get(opp_id)
            if not team or not opp:
                continue

            att_key = "strength_attack_home" if home else "strength_attack_away"
            opp_def_key = "strength_defence_away" if home else "strength_defence_home"
            opp_att_key = "strength_attack_away" if home else "strength_attack_home"
            own_def_key = "strength_defence_home" if home else "strength_defence_away"

            team_att = float(team.get(att_key) or avg_att)
            opp_def = float(opp.get(opp_def_key) or avg_def)
            attack_adj = ((avg_def / opp_def) ** ALPHA) * ((team_att / avg_att) ** ALPHA)

            opp_att = float(opp.get(opp_att_key) or avg_att)
            own_def = float(team.get(own_def_key) or avg_def)
            # League-average goals conceded ~1.35; scaled by relative strength.
            concede = 1.35 * ((opp_att / avg_att) ** ALPHA) * ((avg_def / own_def) ** ALPHA)

            out.setdefault(team_id, []).append(FixtureCtx(
                fixture_id=r["id"], gw=r["event"], team_id=team_id,
                opponent_id=opp_id, is_home=home,
                difficulty=r["team_h_difficulty"] if home else r["team_a_difficulty"],
                attack_adj=round(attack_adj, 4),
                concede_lambda=round(max(0.2, concede), 4),
            ))
    return out


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------
def project_player(conn: sqlite3.Connection, player: dict, gw: int,
                   fixtures: list[FixtureCtx], latest_gw: int,
                   priors: dict[str, tuple], rules: dict | None = None,
                   rotation_score: float = 0.0,
                   understat_ok: bool = True,
                   player_prior: priors_mod.PlayerPrior | None = None,
                   season_minutes: float | None = None) -> XPBreakdown:
    """Project one player for one gameweek across all their fixtures.

    When `player_prior` is supplied (the baselines table has been seeded),
    rate estimation switches from positional shrinkage to Bayesian blending
    against the player's own historical baseline:

        weight = min(1, season_minutes / 720)
        rate   = weight * current + (1 - weight) * prior

    so an N=2 sample cannot dominate a season of last-year evidence, and a
    zero-minute player projects on 100% prior instead of a league average.
    Without a prior the v2 positional-shrinkage path is unchanged.
    """
    rules = rules or load_rules()
    scoring = rules["scoring"]
    etype = player.get("element_type")
    pos = ELEMENT_TYPE_TO_POS.get(etype, "MID") if etype is not None else "MID"
    pid = int(player["id"])

    out = XPBreakdown(player_id=pid, gw=gw, fixtures=len(fixtures))

    mp = minutes_mod.profile(conn, player, latest_gw, rotation_score)
    out.exp_minutes = mp.exp_minutes
    out.p_start = mp.p_start
    out.p_60 = mp.p_60

    if not fixtures:
        out.notes.append("blank gameweek")
        out.recompute_total()
        return out
    if mp.availability <= 0:
        out.notes.append("unavailable")
        out.recompute_total()
        return out

    # -- attacking rates, Understat first ---------------------------------
    understat_id = player.get("understat_id")
    rates = None
    if understat_ok and understat_id:
        rates = _understat_rates(conn, str(understat_id), latest_gw)
    if rates is not None:
        out.source = SOURCE_UNDERSTAT
    else:
        rates = _fpl_rates(conn, pid, latest_gw)
        out.source = SOURCE_FPL
        if understat_id and understat_ok:
            out.notes.append("no understat match rows; using FPL baseline")

    xg90_raw, xa90_raw, sample_min = rates
    prior_xg, prior_xa, prior_dc = priors.get(pos, (0.1, 0.1, 5.0))

    blend_w = None
    if player_prior is not None:
        # Credibility uses raw season minutes, not the recency-weighted sample
        # behind the rate estimate -- evidence volume, not evidence freshness.
        if season_minutes is None:
            row = conn.execute(
                "SELECT SUM(minutes) m FROM player_gw"
                " WHERE player_id = ? AND gw <= ?", (pid, latest_gw)).fetchone()
            season_minutes = float(row["m"] or 0.0) if row else 0.0
        blend_w = priors_mod.blend_weight(season_minutes)
        xg90 = priors_mod.blend(xg90_raw, season_minutes, player_prior.npxg90)
        xa90 = priors_mod.blend(xa90_raw, season_minutes, player_prior.xa90)
        if blend_w < 1.0:
            out.notes.append(
                f"prior {player_prior.source}/{player_prior.season}"
                f" weight={1.0 - blend_w:.2f}")
    else:
        xg90 = _shrink(xg90_raw, sample_min, prior_xg)
        xa90 = _shrink(xa90_raw, sample_min, prior_xa)

    # Set-piece premium from the order columns already ingested in v1.
    premium = 1.0
    if player.get("penalties_order") == 1:
        premium += PEN_PREMIUM
    if (player.get("freekicks_order") or 99) <= 2:
        premium += SET_PIECE_PREMIUM

    if player_prior is not None:
        dc90 = priors_mod.blend(_defcon_rate(conn, pid, latest_gw),
                                season_minutes, player_prior.defcon90)
    else:
        dc90 = _shrink(_defcon_rate(conn, pid, latest_gw), sample_min, prior_dc)
    saves90 = _saves_rate(conn, pid, latest_gw)
    bonus_per_app = _bonus_rate(conn, pid, latest_gw)

    goal_pts = float(scoring["goal"].get(pos, 4))
    assist_pts = float(scoring["assist"])
    cs_pts = float(scoring["clean_sheet"].get(pos, 0))
    dc_cfg = scoring["defensive_contribution"]
    dc_threshold = int(dc_cfg["threshold"].get(pos, 12))
    dc_points = float(dc_cfg["points"])

    variance = 0.0

    for fx in fixtures:
        share = mp.exp_minutes / 90.0
        exp_goals = xg90 * share * fx.attack_adj * premium
        exp_assists = xa90 * share * fx.attack_adj

        out.goals += goal_pts * exp_goals
        out.assists += assist_pts * exp_assists
        variance += (goal_pts ** 2) * exp_goals + (assist_pts ** 2) * exp_assists

        # Appearance
        out.appearance += (
            float(scoring["appearance"]["over_60"]) * mp.p_60
            + float(scoring["appearance"]["under_60"]) * max(0.0, mp.p_appear - mp.p_60)
        )

        # Clean sheet, and the concession penalty for GKP/DEF
        if cs_pts > 0:
            p_cs_fixture = math.exp(-fx.concede_lambda)
            if (player_prior is not None and blend_w is not None
                    and player_prior.xcs_rate > 0):
                # Early season the concede rate itself rests on carried-over
                # FPL strength ratings; blend it toward the player's own
                # historical (or promoted-tier base) clean-sheet rate with the
                # same credibility weight as the attacking rates.
                p_cs_fixture = (blend_w * p_cs_fixture
                                + (1.0 - blend_w) * min(0.6, player_prior.xcs_rate))
            p_cs = p_cs_fixture * mp.p_60
            out.clean_sheet += cs_pts * p_cs
            variance += (cs_pts ** 2) * p_cs * (1.0 - p_cs)

        if pos in ("GKP", "DEF"):
            penalty = sum(
                (k // 2) * minutes_mod.poisson_pmf(fx.concede_lambda, k)
                for k in range(2, 9)
            )
            out.conceded += penalty * mp.p_60

        if pos == "GKP":
            out.saves += (saves90 * share) / 3.0

        # Defensive contribution: Poisson tail over the per-90 action rate.
        if dc90 > 0 and dc_threshold > 0:
            lam = dc90 * share
            out.defcon += dc_points * minutes_mod.poisson_at_least(lam, dc_threshold)

        out.bonus += bonus_per_app * mp.p_appear
        out.cards += 0.09 * mp.p_appear  # league-average yellow rate

    out.recompute_total()
    out.variance = round(variance, 4)

    sd = math.sqrt(max(variance, 0.01))
    out.p_haul_12 = round(_tail(out.total, sd, 12.0), 4)
    out.p_floor_5 = round(_tail(out.total, sd, 5.0), 4)

    for name in ("goals", "assists", "clean_sheet", "saves", "defcon",
                 "bonus", "conceded", "cards", "appearance"):
        setattr(out, name, round(getattr(out, name), 4))
    out.recompute_total()
    return out


def _tail(mean: float, sd: float, threshold: float) -> float:
    """P(X >= threshold), normal approximation with a continuity correction.

    Crude for a heavily skewed distribution, but it only ever ranks candidates
    against each other, and the ordering it produces is stable.
    """
    if sd <= 0:
        return 1.0 if mean >= threshold else 0.0
    z = (threshold - 0.5 - mean) / sd
    return max(0.0, min(1.0, 0.5 * math.erfc(z / math.sqrt(2.0))))


def project(conn: sqlite3.Connection, gws: list[int],
            player_ids: list[int] | None = None,
            rules: dict | None = None,
            understat_ok: bool = True,
            persist: bool = True,
            as_of: int | None = None,
            neutralise_availability: bool = False,
            ) -> dict[tuple[int, int], XPBreakdown]:
    """Project every player over `gws`. Returns {(player_id, gw): breakdown}.

    `as_of` pins the last gameweek whose results may inform the forecast. It
    defaults to MAX(player_gw.gw), which is what live planning wants. Backtests
    must pass `as_of = target_gw - 1` explicitly: without it a projection for an
    already-played gameweek reads that gameweek's own results and scores itself.

    `neutralise_availability` blanks the live injury fields on the player row.
    Those columns are a *current* snapshot with no history behind them, so
    replaying a past gameweek would otherwise apply today's injuries to it --
    lookahead in one direction and stale nonsense in the other. Live callers
    leave it off; backtests turn it on so minutes come purely from history.
    """
    rules = rules or load_rules()
    if as_of is None:
        state_row = conn.execute(
            "SELECT MAX(gw) g FROM player_gw"
        ).fetchone()
        as_of = int(state_row["g"] or 0) if state_row else 0
    latest_gw = int(as_of)

    sql = """SELECT id, element_type, team_id, status, news,
                    chance_of_playing_next_round, understat_id,
                    penalties_order, freekicks_order, now_cost, web_name
             FROM players"""
    params: list = []
    if player_ids:
        sql += f" WHERE id IN ({','.join('?' * len(player_ids))})"
        params = list(player_ids)
    players = [dict(r) for r in conn.execute(sql, params)]
    if neutralise_availability:
        for player in players:
            player["status"] = "a"
            player["news"] = None
            player["chance_of_playing_next_round"] = None

    priors = _positional_priors(conn, latest_gw)
    player_priors = priors_mod.load_priors(conn)
    season_minutes = (priors_mod.current_season_minutes(conn, latest_gw)
                      if player_priors else {})
    by_team = fixture_contexts(conn, gws)
    run_id = uuid.uuid4().hex[:12]
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    results: dict[tuple[int, int], XPBreakdown] = {}
    for player in players:
        team_id = player.get("team_id")
        team_fixtures = by_team.get(team_id, []) if team_id is not None else []
        pid = int(player["id"])
        for gw in gws:
            fx = [f for f in team_fixtures if f.gw == gw]
            bd = project_player(conn, player, gw, fx, latest_gw, priors,
                                rules, understat_ok=understat_ok,
                                player_prior=player_priors.get(pid),
                                season_minutes=season_minutes.get(pid, 0.0))
            results[(bd.player_id, gw)] = bd

    if persist and results:
        _persist(conn, results, run_id, now)
    return results


def _persist(conn: sqlite3.Connection, results: dict, run_id: str, now: str) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO xp_projection
             (player_id, gw, run_id, fixtures, exp_minutes, p_start, p_60,
              xp_appearance, xp_goals, xp_assists, xp_clean_sheet, xp_saves,
              xp_defcon, xp_bonus, xp_conceded, xp_cards, xp_total, xp_variance,
              p_haul_12, p_floor_5, source, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (b.player_id, b.gw, run_id, b.fixtures, b.exp_minutes, b.p_start, b.p_60,
             b.appearance, b.goals, b.assists, b.clean_sheet, b.saves, b.defcon,
             b.bonus, b.conceded, b.cards, b.total, b.variance,
             b.p_haul_12, b.p_floor_5, b.source, now)
            for b in results.values()
        ],
    )
    conn.commit()


def as_dict(breakdown: XPBreakdown) -> dict:
    return asdict(breakdown)
