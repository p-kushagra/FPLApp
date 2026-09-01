"""Live matchday: provisional bonus, auto-substitutions and rank threat.

Three things change during a gameweek that no pre-deadline model can tell you,
and all three are decision-relevant while matches are still running:

* **Provisional bonus.** FPL awards bonus from the BPS ranking within each
  match, but only after the match is finished and verified. During play the BPS
  numbers are live and the bonus is not, so the top three by BPS in each fixture
  are computed here and clearly labelled *provisional* -- they move, sometimes
  in the 89th minute.
* **Auto-substitutions.** A starter on zero minutes is replaced at the final
  whistle of the gameweek by the first eligible bench player, subject to
  formation legality. Simulating that mid-gameweek is the difference between
  "I scored 41" and "I will score 47 once the subs land".
* **Rank threat.** A rival's captain hauling costs you rank whether or not you
  own the player. The ILEO swing already computes this pre-deadline; here it is
  applied to points that have actually been scored.

Everything degrades: with no live feed the module returns the same shapes built
from stored `player_gw` rows, so the page renders a settled gameweek rather than
an error.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .rules import ELEMENT_TYPE_TO_POS, load_rules

# Bonus awarded to the top three BPS scorers in each match. Ties share the
# higher award, which is why this is a rank walk rather than a slice.
BONUS_AWARDS = (3, 2, 1)

# FPL formation legality for the starting XI.
MIN_FORMATION = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
MAX_FORMATION = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}
SQUAD_SIZE = 15
XI_SIZE = 11


@dataclass
class LivePlayer:
    """One player's live state this gameweek."""

    player_id: int
    name: str = ""
    team: str = ""
    position: str = "MID"
    minutes: int = 0
    total_points: int = 0
    bps: int = 0
    goals: int = 0
    assists: int = 0
    clean_sheet: bool = False
    bonus_awarded: int = 0
    provisional_bonus: int = 0
    fixture_id: int | None = None
    fixture_finished: bool = False

    @property
    def played(self) -> bool:
        return self.minutes > 0

    @property
    def live_points(self) -> int:
        """Points including provisional bonus that has not been confirmed yet."""
        if self.fixture_finished or self.bonus_awarded:
            return self.total_points
        return self.total_points + self.provisional_bonus


@dataclass
class Substitution:
    """One auto-sub the engine would apply at the final whistle."""

    out_id: int
    out_name: str
    out_position: str
    in_id: int
    in_name: str
    in_position: str
    points_gained: int = 0
    reason: str = ""


@dataclass
class ThreatRow:
    """A player whose live points are moving your rank against the field."""

    player_id: int
    name: str
    team: str
    live_points: int
    my_multiplier: float
    ileo: float
    swing: float
    owned_by: int = 0
    rival_count: int = 0

    @property
    def net_swing(self) -> float:
        """Points gained (positive) or lost (negative) against the rival field."""
        return round(self.swing * self.live_points, 2)

    @property
    def verdict(self) -> str:
        if self.net_swing > 0.5:
            return "gaining"
        if self.net_swing < -0.5:
            return "bleeding"
        return "neutral"


@dataclass
class LiveState:
    """Everything the Live Matchday page renders."""

    gw: int
    players: dict[int, LivePlayer] = field(default_factory=dict)
    squad: list[LivePlayer] = field(default_factory=list)
    subs: list[Substitution] = field(default_factory=list)
    threats: list[ThreatRow] = field(default_factory=list)
    provisional_points: int = 0
    settled_points: int = 0
    captain_id: int | None = None
    vice_id: int | None = None
    vice_activated: bool = False
    active_chip: str | None = None
    fixtures_finished: int = 0
    fixtures_total: int = 0
    source: str = "stored"
    notes: list[str] = field(default_factory=list)

    @property
    def in_progress(self) -> bool:
        return self.fixtures_total > 0 and self.fixtures_finished < self.fixtures_total

    @property
    def points_after_subs(self) -> int:
        return self.provisional_points + sum(s.points_gained for s in self.subs)

    @property
    def net_threat(self) -> float:
        return round(sum(t.net_swing for t in self.threats), 2)


# --------------------------------------------------------------------------
# Provisional bonus
# --------------------------------------------------------------------------
def provisional_bonus(bps_by_fixture: dict[int, list[tuple[int, int]]]
                      ) -> dict[int, int]:
    """`{player_id: bonus}` from `{fixture_id: [(player_id, bps), ...]}`.

    Ties take the higher award and consume the ranks below them, which is FPL's
    actual rule: two players tied on top BPS both get 3, and the next gets 1.
    """
    out: dict[int, int] = {}
    for rows in bps_by_fixture.values():
        ranked = sorted((r for r in rows if r[1] > 0),
                        key=lambda r: r[1], reverse=True)
        if not ranked:
            continue
        awarded = 0
        index = 0
        while index < len(ranked) and awarded < len(BONUS_AWARDS):
            score = ranked[index][1]
            tied = [r for r in ranked if r[1] == score]
            points = BONUS_AWARDS[awarded]
            for pid, _bps in tied:
                out[pid] = points
            awarded += len(tied)
            index += len(tied)
    return out


# --------------------------------------------------------------------------
# Auto-substitutions
# --------------------------------------------------------------------------
def _formation_of(positions: list[str]) -> dict[str, int]:
    counts = dict.fromkeys(MIN_FORMATION, 0)
    for pos in positions:
        counts[pos] = counts.get(pos, 0) + 1
    return counts


def formation_legal(positions: list[str]) -> bool:
    """Is this set of starting positions a legal XI?"""
    if len(positions) != XI_SIZE:
        return False
    counts = _formation_of(positions)
    return all(MIN_FORMATION[p] <= counts.get(p, 0) <= MAX_FORMATION[p]
               for p in MIN_FORMATION)


def auto_subs(starters: list[dict], bench: list[dict]) -> list[Substitution]:
    """Apply FPL's auto-substitution rules to a finished gameweek.

    Each entry needs `player_id`, `name`, `position`, `minutes` and `points`.
    `bench` must already be in the manager's chosen bench order -- that order is
    the priority, and re-sorting it silently changes the answer.

    The rules, in the order they bind:

    1. Only a starter who played **zero** minutes is replaced.
    2. A goalkeeper can only ever be replaced by the bench goalkeeper.
    3. An outfield replacement is the first bench player who played and whose
       introduction leaves the XI formation legal.
    4. A bench player can only come on once.
    """
    subs: list[Substitution] = []
    used: set[int] = set()
    current = [dict(s) for s in starters]

    for index, starter in enumerate(current):
        if starter.get("minutes", 0) > 0:
            continue
        is_keeper = starter["position"] == "GKP"

        for candidate in bench:
            if candidate["player_id"] in used:
                continue
            if candidate.get("minutes", 0) <= 0:
                continue
            if is_keeper != (candidate["position"] == "GKP"):
                # Keepers swap only with keepers, and never with outfielders.
                continue

            trial = [p["position"] for j, p in enumerate(current) if j != index]
            trial.append(candidate["position"])
            if not formation_legal(trial):
                continue

            subs.append(Substitution(
                out_id=starter["player_id"], out_name=starter.get("name", ""),
                out_position=starter["position"],
                in_id=candidate["player_id"], in_name=candidate.get("name", ""),
                in_position=candidate["position"],
                points_gained=int(candidate.get("points", 0)),
                reason=(f"{starter.get('name', 'starter')} did not play; "
                        f"{candidate.get('name', 'bench')} comes on")))
            used.add(candidate["player_id"])
            current[index] = {**candidate}
            break

    return subs


def vice_takes_over(captain: dict | None, vice: dict | None) -> bool:
    """The vice-captain inherits the armband only if the captain played zero."""
    if captain is None or vice is None:
        return False
    return captain.get("minutes", 0) == 0 and vice.get("minutes", 0) > 0


# --------------------------------------------------------------------------
# Live ingest
# --------------------------------------------------------------------------
def _stored_state(conn: sqlite3.Connection, gw: int) -> dict[int, LivePlayer]:
    """Rebuild live shapes from `player_gw`, for a settled or offline gameweek."""
    out: dict[int, LivePlayer] = {}
    for r in conn.execute(
            """SELECT g.player_id, g.minutes, g.total_points, g.bps, g.bonus,
                      g.goals_scored, g.assists, g.clean_sheets, g.fixture_id,
                      p.web_name, p.element_type, t.short_name AS team
               FROM player_gw g
               JOIN players p ON p.id = g.player_id
               LEFT JOIN teams t ON t.id = p.team_id
               WHERE g.gw = ?""", (gw,)):
        pid = int(r["player_id"])
        out[pid] = LivePlayer(
            player_id=pid, name=r["web_name"] or "", team=r["team"] or "",
            position=ELEMENT_TYPE_TO_POS.get(r["element_type"], "MID"),
            minutes=int(r["minutes"] or 0),
            total_points=int(r["total_points"] or 0),
            bps=int(r["bps"] or 0), goals=int(r["goals_scored"] or 0),
            assists=int(r["assists"] or 0),
            clean_sheet=bool(r["clean_sheets"]),
            bonus_awarded=int(r["bonus"] or 0),
            fixture_id=r["fixture_id"], fixture_finished=True)
    return out


def _live_state(conn: sqlite3.Connection, gw: int,
                elements: list[dict]) -> dict[int, LivePlayer]:
    """Parse an `/event/{gw}/live/` payload into live players."""
    meta = {int(r["id"]): r for r in conn.execute(
        """SELECT p.id, p.web_name, p.element_type, t.short_name AS team
           FROM players p LEFT JOIN teams t ON t.id = p.team_id""")}
    finished = {int(r["id"]): bool(r["finished"]) for r in conn.execute(
        "SELECT id, finished FROM fixtures WHERE event = ?", (gw,))}

    out: dict[int, LivePlayer] = {}
    for element in elements:
        pid = int(element.get("id", 0))
        stats = element.get("stats") or {}
        info = meta.get(pid)
        explain = element.get("explain") or []
        fixture_id = None
        if explain and isinstance(explain[0], dict):
            fixture_id = explain[0].get("fixture")

        out[pid] = LivePlayer(
            player_id=pid,
            name=(info["web_name"] if info else "") or "",
            team=(info["team"] if info else "") or "",
            position=ELEMENT_TYPE_TO_POS.get(
                info["element_type"] if info else None, "MID"),
            minutes=int(stats.get("minutes") or 0),
            total_points=int(stats.get("total_points") or 0),
            bps=int(stats.get("bps") or 0),
            goals=int(stats.get("goals_scored") or 0),
            assists=int(stats.get("assists") or 0),
            clean_sheet=bool(stats.get("clean_sheets")),
            bonus_awarded=int(stats.get("bonus") or 0),
            fixture_id=fixture_id,
            fixture_finished=finished.get(fixture_id, False) if fixture_id else False)
    return out


def build(conn: sqlite3.Connection, gw: int, *,
          elements: list[dict] | None = None,
          fetch: bool = True,
          rival_swings: dict[int, tuple[float, float, int, int]] | None = None
          ) -> LiveState:
    """Assemble the full live view for `gw`.

    `elements` lets a caller (or a test) inject a payload. Otherwise the live
    endpoint is fetched through the cached, non-raising source adapter, and a
    failure falls back to stored history rather than raising.
    """
    state = LiveState(gw=gw)
    load_rules()

    if elements is None and fetch:
        try:
            from .sources.fpl import FplSource
            result = FplSource(conn).live(gw)
            if result.usable and isinstance(result.data, dict):
                elements = result.data.get("elements") or []
                state.source = f"live:{result.quality.value}"
            else:
                state.notes.append(
                    f"live feed unavailable ({result.error or result.quality}); "
                    "showing stored results")
        except Exception as exc:                      # never break the page
            state.notes.append(f"live feed error: {exc}; showing stored results")

    if elements:
        state.players = _live_state(conn, gw, elements)
    else:
        state.players = _stored_state(conn, gw)
        if state.source == "stored":
            state.notes.append("no live feed - built from stored gameweek data")

    fixtures = conn.execute(
        "SELECT COUNT(*) n, SUM(finished) f FROM fixtures WHERE event = ?",
        (gw,)).fetchone()
    state.fixtures_total = int(fixtures["n"] or 0) if fixtures else 0
    state.fixtures_finished = int(fixtures["f"] or 0) if fixtures else 0

    _apply_provisional_bonus(state)
    _apply_squad(conn, state, gw)
    if rival_swings:
        _apply_threats(state, rival_swings)
    return state


def _apply_provisional_bonus(state: LiveState) -> None:
    by_fixture: dict[int, list[tuple[int, int]]] = {}
    for player in state.players.values():
        if player.fixture_id is None or player.fixture_finished:
            continue
        by_fixture.setdefault(int(player.fixture_id), []).append(
            (player.player_id, player.bps))

    for pid, bonus in provisional_bonus(by_fixture).items():
        if pid in state.players:
            state.players[pid].provisional_bonus = bonus


def _apply_squad(conn: sqlite3.Connection, state: LiveState, gw: int) -> None:
    """Attach your picks, run the auto-sub simulator and total the gameweek."""
    picks = conn.execute(
        """SELECT player_id, position, multiplier, is_captain, is_vice, chip
           FROM my_picks WHERE gw = ? ORDER BY position""", (gw,)).fetchall()
    if not picks:
        state.notes.append("no squad stored for this gameweek")
        return

    state.active_chip = next((p["chip"] for p in picks if p["chip"]), None)

    starters, bench = [], []
    for pick in picks:
        pid = int(pick["player_id"])
        player = state.players.get(pid)
        if player is None:
            continue
        state.squad.append(player)
        multiplier = float(pick["multiplier"] or 0)
        entry = {"player_id": pid, "name": player.name,
                 "position": player.position, "minutes": player.minutes,
                 "points": player.live_points, "multiplier": multiplier,
                 "order": int(pick["position"] or 0)}
        if pick["is_captain"]:
            state.captain_id = pid
        if pick["is_vice"]:
            state.vice_id = pid
        # Bench Boost makes every pick a starter, so bench order is irrelevant
        # and no auto-substitution can occur.
        (starters if multiplier > 0 else bench).append(entry)

    bench.sort(key=lambda e: e["order"])

    # The multiplier from the picks payload is authoritative -- 3 under Triple
    # Captain, 2 for a normal captain -- and is never re-derived from
    # `is_captain`, which is what made v1 score a TC gameweek at 2x.
    state.provisional_points = sum(
        int(e["points"] * e["multiplier"]) for e in starters)
    state.settled_points = sum(
        int(state.players[e["player_id"]].total_points * e["multiplier"])
        for e in starters if e["player_id"] in state.players)

    if state.active_chip != "bboost" and len(starters) == XI_SIZE:
        state.subs = auto_subs(starters, bench)

    captain = next((e for e in starters if e["player_id"] == state.captain_id), None)
    vice = next((e for e in starters + bench
                 if e["player_id"] == state.vice_id), None)
    state.vice_activated = vice_takes_over(captain, vice)
    if state.vice_activated and vice is not None:
        state.notes.append(
            f"Captain did not play - the armband passes to {vice['name']}")


def _apply_threats(state: LiveState,
                   rival_swings: dict[int, tuple[float, float, int, int]]) -> None:
    """Rank hazard from live points, using pre-computed ILEO swings."""
    rows: list[ThreatRow] = []
    for pid, (my_mult, ileo, owned_by, rival_count) in rival_swings.items():
        player = state.players.get(pid)
        if player is None or player.live_points == 0:
            continue
        rows.append(ThreatRow(
            player_id=pid, name=player.name, team=player.team,
            live_points=player.live_points, my_multiplier=my_mult,
            ileo=round(ileo, 3), swing=round(my_mult - ileo, 3),
            owned_by=owned_by, rival_count=rival_count))

    rows.sort(key=lambda r: abs(r.net_swing), reverse=True)
    state.threats = rows
