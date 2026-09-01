"""Captaincy decision matrix: Shield vs Sword.

Ranking captains by expected points alone is a category error. The captain pick
is a RANK decision, not a points decision: against a field that mostly captains
the same premium, matching them is worth more than a marginally higher mean,
and against a deficit that expected points cannot close, only variance can.

    Shield_p = ILEO_cap_p          x  P(points >= floor)
    Sword_p  = (1 - ILEO_cap_p)    x  P(points >= haul)

Which index governs is a function of league state, not taste (see `regime`).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

FLOOR = 5.0    # a "safe return"
HAUL = 12.0    # a genuine haul

# Weekly SD of a head-to-head lead with typical squad overlap. Below this,
# variance alone closes the gap and taking risk is negative EV on rank.
EDGE_THRESHOLD = 2.0


class Regime(str, Enum):
    SHIELD = "shield"
    SWORD = "sword"


@dataclass(frozen=True)
class CaptainOption:
    player_id: int
    web_name: str
    team_short: str
    position: str
    xp: float
    ileo_cap: float
    p_haul: float
    p_floor: float
    fixtures: int
    shield: float
    shield_rank: int = 0
    sword: float = 0.0
    sword_rank: int = 0

    @property
    def classification(self) -> str:
        return "Shield" if self.shield >= self.sword else "Sword"


@dataclass(frozen=True)
class RegimeCall:
    regime: Regime
    deficit: int
    gameweeks_left: int
    required_edge: float
    reason: str


def regime(deficit_points: int, gameweeks_left: int,
           threshold: float = EDGE_THRESHOLD) -> RegimeCall:
    """Decide whether to protect a lead or chase a deficit.

    `deficit_points` is points BEHIND the rival being chased; negative means
    leading.
    """
    gws = max(1, int(gameweeks_left))
    edge = deficit_points / gws

    if deficit_points < 0:
        return RegimeCall(Regime.SHIELD, deficit_points, gws, edge,
                          f"Leading by {abs(deficit_points)}. Protect: match the "
                          f"field's exposure rather than inviting variance.")
    if edge < threshold:
        return RegimeCall(Regime.SHIELD, deficit_points, gws, edge,
                          f"Need {edge:.2f} pts/GW over {gws} GWs. Below the "
                          f"{threshold:.1f} noise floor, so form closes this "
                          f"without taking unmatched risk.")
    return RegimeCall(Regime.SWORD, deficit_points, gws, edge,
                      f"Need {edge:.2f} pts/GW over {gws} GWs. Above the "
                      f"{threshold:.1f} noise floor: the EV-maximising pick is "
                      f"provably insufficient, so variance is the only route.")


def _rank(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda i: -values[i])
    ranks = [0] * len(values)
    for position, idx in enumerate(order, start=1):
        ranks[idx] = position
    return ranks


def matrix(conn: sqlite3.Connection, gw: int,
           candidate_ids: list[int] | None = None,
           ileo_cap: dict[int, float] | None = None,
           run_id: str | None = None,
           limit: int = 20) -> list[CaptainOption]:
    """Build the Shield/Sword matrix from stored xP projections."""
    ileo_cap = ileo_cap or {}

    sql = """SELECT xp.player_id, xp.xp_total, xp.p_haul_12, xp.p_floor_5,
                    xp.fixtures, p.web_name, p.position, p.element_type,
                    t.short_name AS team_short
             FROM xp_projection xp
             JOIN players p ON p.id = xp.player_id
             LEFT JOIN teams t ON t.id = p.team_id
             WHERE xp.gw = ?"""
    params: list = [gw]
    if run_id:
        sql += " AND xp.run_id = ?"
        params.append(run_id)
    else:
        sql += """ AND xp.run_id = (SELECT run_id FROM xp_projection
                                    WHERE gw = ? ORDER BY computed_at DESC LIMIT 1)"""
        params.append(gw)
    if candidate_ids:
        sql += f" AND xp.player_id IN ({','.join('?' * len(candidate_ids))})"
        params.extend(candidate_ids)
    sql += " ORDER BY xp.xp_total DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(sql, params)]
    if not rows:
        return []

    shields, swords = [], []
    for r in rows:
        eo = float(ileo_cap.get(r["player_id"], 0.0))
        shields.append(eo * float(r["p_floor_5"] or 0.0))
        swords.append((1.0 - eo) * float(r["p_haul_12"] or 0.0))

    shield_ranks = _rank(shields)
    sword_ranks = _rank(swords)

    out = []
    for i, r in enumerate(rows):
        out.append(CaptainOption(
            player_id=r["player_id"],
            web_name=r["web_name"] or "?",
            team_short=r["team_short"] or "?",
            position=r["position"] or "?",
            xp=round(float(r["xp_total"] or 0.0), 2),
            ileo_cap=round(float(ileo_cap.get(r["player_id"], 0.0)), 3),
            p_haul=round(float(r["p_haul_12"] or 0.0), 3),
            p_floor=round(float(r["p_floor_5"] or 0.0), 3),
            fixtures=int(r["fixtures"] or 0),
            shield=round(shields[i], 4),
            shield_rank=shield_ranks[i],
            sword=round(swords[i], 4),
            sword_rank=sword_ranks[i],
        ))
    return out


def recommend(options: list[CaptainOption], call: RegimeCall
              ) -> tuple[CaptainOption | None, str]:
    """Pick a captain under the governing regime, with the reasoning."""
    if not options:
        return None, "No projections available for this gameweek."

    if call.regime is Regime.SHIELD:
        pick = max(options, key=lambda o: (o.shield, o.xp))
        alt = max((o for o in options if o.player_id != pick.player_id),
                  key=lambda o: o.sword, default=None)
        why = (f"{pick.web_name} (C). {call.reason} "
               f"Shield {pick.shield:.2f} on {pick.ileo_cap:.0%} captain EO "
               f"with a {pick.p_floor:.0%} floor.")
        if alt and alt.sword > pick.sword:
            why += (f" {alt.web_name} offers +{alt.sword - pick.sword:.2f} Sword, "
                    f"but {1 - alt.ileo_cap:.0%} of the field is unmatched downside.")
    else:
        pick = max(options, key=lambda o: (o.sword, o.xp))
        why = (f"{pick.web_name} (C). {call.reason} "
               f"Sword {pick.sword:.2f}: {pick.p_haul:.0%} haul chance at only "
               f"{pick.ileo_cap:.0%} captain EO.")

    return pick, why
