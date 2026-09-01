"""Role arbitrage: players whose deployed role beats their FPL classification.

FPL scores by *listed* position, never by where a player actually lines up:

    Goal        GKP/DEF 6   MID 5   FWD 4
    Clean sheet GKP/DEF 4   MID 1   FWD 0
    Assist                  3 for everyone

So a midfielder playing as a centre-forward banks 5 points a goal instead of 4
*and* keeps a clean-sheet point; a defender pushed up the wing banks 6 and keeps
the 4-point clean sheet while priced as a defender. Both are genuine mispricings,
and both are usually temporary -- the role exists because someone ahead of them
is missing.

This module detects three distinct exploits, each with its own evidence test:

* **OOP midfielder** (`[OOP: Striker]`, `[OOP: Inside Fwd]`) -- a MID whose
  shot volume and box proximity match forwards rather than midfielders.
* **Attacking wing-back** (`[Attacking WB]`) -- a DEF with forward-grade box
  threat and chance creation, carrying a defender's clean-sheet points.
* **Set-piece monopoliser** (`[Penalties]`, `[Free Kicks]`, `[Corners]`) -- read
  straight from FPL's own order columns; no inference required, and the single
  most reliable points premium in the game.

Detection is inference, because the API never states where a player lined up.
The guard against false positives is requiring *two* signals that move in
opposite directions: attacking output well above positional peers AND defensive
workload well below. A centre-back who scored twice from corners trips the first
test and fails the second, which is the correct answer.

Box proximity and crossing volume are not exposed by the FPL API. `threat` is
built from shot location and quality, so it is the honest proxy for box touches;
`creativity` is built from chance creation including crosses. Both are used as
*relative* measures against a positional median, never as absolute quantities,
which is what keeps the proxy defensible.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field

MIN_MINUTES = 60           # a cameo says nothing about a player's role
BASELINE_MIN_SAMPLE = 8    # players needed before a positional median is meaningful
MIN_SAMPLE_APPS = 2        # appearances before a role claim is made at all

# FPL points by listed position.
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
ASSIST_POINTS = 3

# Detection thresholds. Deliberately strict: a false "he's playing as a striker"
# costs a transfer, whereas a missed one costs only the edge we never had.
OOP_ATTACK_RATIO = 1.8     # vs the median for the player's listed position
OOP_DEFENCE_RATIO = 0.75   # defensive workload must also be *below* peers
WINGBACK_THREAT_RATIO = 1.8
WINGBACK_CREATIVITY_RATIO = 1.5

# Badge vocabulary. Rendered verbatim by the pitch and player cards.
BADGE_OOP_STRIKER = "[OOP: Striker]"
BADGE_OOP_INSIDE = "[OOP: Inside Fwd]"
BADGE_WINGBACK = "[Attacking WB]"
BADGE_PENALTIES = "[Penalties]"
BADGE_FREEKICKS = "[Free Kicks]"
BADGE_CORNERS = "[Corners]"

ROLE_AS_LISTED = "as listed"
ROLE_ADVANCED = "advanced"
ROLE_DEEPER = "deeper"


def _per90(value, minutes) -> float:
    return (float(value or 0) * 90.0 / float(minutes)) if minutes else 0.0


def _ratio(value: float, baseline: float) -> float:
    """Guarded division. A zero baseline means 'no evidence', not 'infinite'."""
    return (value / baseline) if baseline else 0.0


@dataclass
class RoleProfile:
    """One player's deployed role, the badges it earns and what it is worth."""

    player_id: int
    player: str = ""
    team: str = ""
    team_id: int | None = None
    position: str = "MID"
    cost: float = 0.0
    ownership: float = 0.0
    status: str = "a"
    minutes: int = 0
    sample: int = 0

    threat90: float = 0.0
    creativity90: float = 0.0
    xgi90: float = 0.0
    xg90: float = 0.0
    defensive90: float = 0.0

    attack_ratio: float = 0.0
    creativity_ratio: float = 0.0
    defence_ratio: float = 0.0

    role: str = ROLE_AS_LISTED
    oop_striker: bool = False
    oop_inside_forward: bool = False
    attacking_wingback: bool = False
    on_penalties: bool = False
    on_freekicks: bool = False
    on_corners: bool = False

    premium_per90: float = 0.0
    compared_to: str | None = None
    badges: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_arbitrage(self) -> bool:
        """True when the listing itself is the edge, set pieces aside."""
        return self.oop_striker or self.oop_inside_forward or self.attacking_wingback

    @property
    def set_piece_badges(self) -> list[str]:
        return [b for b in self.badges
                if b in (BADGE_PENALTIES, BADGE_FREEKICKS, BADGE_CORNERS)]

    def badge_text(self) -> str:
        return " ".join(self.badges)


# --------------------------------------------------------------------------
# Positional baselines
# --------------------------------------------------------------------------
def position_baselines(conn: sqlite3.Connection) -> dict[str, dict]:
    """Median per-90 output for each listed position.

    Defensive workload uses clearances/blocks/interceptions rather than tackles.
    CBI is positional -- it happens defending your own box -- whereas tackles
    happen all over the pitch, so including them blurs the exact distinction
    being measured.
    """
    rows = conn.execute(
        """SELECT p.position, g.minutes, g.threat, g.creativity,
                  g.expected_goal_involvements AS xgi, g.expected_goals AS xg,
                  g.clearances_blocks_interceptions AS cbi
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE g.minutes >= ?""",
        (MIN_MINUTES,),
    ).fetchall()

    buckets: dict[str, dict[str, list]] = {}
    for r in rows:
        pos = r["position"]
        if not pos:
            continue
        b = buckets.setdefault(pos, {"threat": [], "creativity": [], "xgi": [],
                                     "xg": [], "def": []})
        b["threat"].append(_per90(r["threat"], r["minutes"]))
        b["creativity"].append(_per90(r["creativity"], r["minutes"]))
        b["xgi"].append(_per90(r["xgi"], r["minutes"]))
        b["xg"].append(_per90(r["xg"], r["minutes"]))
        b["def"].append(_per90(r["cbi"], r["minutes"]))

    out: dict[str, dict] = {}
    for pos, b in buckets.items():
        if len(b["threat"]) < BASELINE_MIN_SAMPLE:
            continue
        out[pos] = {
            "threat": statistics.median(b["threat"]),
            "creativity": statistics.median(b["creativity"]),
            "xgi": statistics.median(b["xgi"]),
            "xg": statistics.median(b["xg"]),
            "defensive": statistics.median(b["def"]),
            "sample": len(b["threat"]),
        }
    return out


# --------------------------------------------------------------------------
# Role inference
# --------------------------------------------------------------------------
_PROFILE_SQL = """
    SELECT p.id, p.web_name, p.position, p.element_type, p.now_cost,
           p.selected_by_percent, p.status, p.team_id,
           p.corners_order, p.freekicks_order, p.penalties_order,
           t.short_name AS team_short,
           SUM(g.minutes) mins, SUM(g.threat) threat, SUM(g.creativity) creativity,
           SUM(g.expected_goal_involvements) xgi, SUM(g.expected_goals) xg,
           SUM(g.clearances_blocks_interceptions) cbi,
           SUM(g.total_points) pts, COUNT(*) apps
    FROM players p
    LEFT JOIN teams t ON t.id = p.team_id
    JOIN player_gw g ON g.player_id = p.id
    WHERE p.id = ? AND g.minutes >= ?
    GROUP BY p.id
"""


def role_profile(conn: sqlite3.Connection, player_id: int,
                 baselines: dict | None = None) -> RoleProfile:
    """Classify one player's deployed role against their positional peers."""
    baselines = baselines if baselines is not None else position_baselines(conn)
    row = conn.execute(_PROFILE_SQL, (player_id, MIN_MINUTES)).fetchone()

    if row is None or not row["mins"]:
        return RoleProfile(player_id=player_id, sample=0)

    pos = row["position"] or "MID"
    mins = float(row["mins"])
    profile = RoleProfile(
        player_id=player_id,
        player=row["web_name"] or "",
        team=row["team_short"] or "",
        team_id=row["team_id"],
        position=pos,
        cost=float(row["now_cost"] or 0.0),
        ownership=float(row["selected_by_percent"] or 0.0),
        status=row["status"] or "a",
        minutes=int(mins),
        sample=int(row["apps"] or 0),
        threat90=round(_per90(row["threat"], mins), 1),
        creativity90=round(_per90(row["creativity"], mins), 1),
        xgi90=round(_per90(row["xgi"], mins), 3),
        xg90=round(_per90(row["xg"], mins), 3),
        defensive90=round(_per90(row["cbi"], mins), 1),
        on_penalties=row["penalties_order"] is not None,
        on_freekicks=(row["freekicks_order"] or 99) <= 2,
        on_corners=row["corners_order"] is not None,
    )

    _apply_set_piece_badges(profile)

    base = baselines.get(pos)
    if base is None or profile.sample < MIN_SAMPLE_APPS:
        profile.notes.append("not enough evidence to judge deployed role")
        return profile

    profile.attack_ratio = round(max(
        _ratio(profile.threat90, base["threat"]),
        _ratio(profile.xgi90, base["xgi"])), 2)
    profile.creativity_ratio = round(
        _ratio(profile.creativity90, base["creativity"]), 2)
    profile.defence_ratio = round(
        _ratio(profile.defensive90, base["defensive"]), 2)

    _classify(profile, base)
    _price_the_mismatch(profile)
    return profile


def _apply_set_piece_badges(profile: RoleProfile) -> None:
    """Set-piece duty is stated by the API, so it needs no inference at all."""
    if profile.on_penalties:
        profile.badges.append(BADGE_PENALTIES)
    if profile.on_freekicks:
        profile.badges.append(BADGE_FREEKICKS)
    if profile.on_corners:
        profile.badges.append(BADGE_CORNERS)


def _classify(profile: RoleProfile, base: dict) -> None:
    """Decide the deployed role from the attack/defence signal pair."""
    advanced = (profile.attack_ratio >= OOP_ATTACK_RATIO
                and profile.defence_ratio <= OOP_DEFENCE_RATIO)

    if advanced and profile.position == "MID":
        profile.role = ROLE_ADVANCED
        # A striker's signature is shot volume (xG), an inside forward's is
        # overall involvement. Splitting them matters because only the striker
        # reading justifies expecting a forward's goal share.
        if _ratio(profile.xg90, base["xg"]) >= OOP_ATTACK_RATIO:
            profile.oop_striker = True
            profile.badges.insert(0, BADGE_OOP_STRIKER)
        else:
            profile.oop_inside_forward = True
            profile.badges.insert(0, BADGE_OOP_INSIDE)

    elif advanced and profile.position == "DEF":
        profile.role = ROLE_ADVANCED
        # A wing-back has to show *both* box threat and crossing volume. Threat
        # alone is a centre-back who attacks corners, which is not the same
        # asset and does not survive the first clean sheet.
        if (profile.attack_ratio >= WINGBACK_THREAT_RATIO
                and profile.creativity_ratio >= WINGBACK_CREATIVITY_RATIO):
            profile.attacking_wingback = True
            profile.badges.insert(0, BADGE_WINGBACK)
        else:
            profile.notes.append(
                "advanced output but no crossing volume - set-piece threat, "
                "not a wing-back role")

    elif (profile.attack_ratio <= 0.5
          and profile.defence_ratio >= 1.4):
        profile.role = ROLE_DEEPER
    else:
        profile.role = ROLE_AS_LISTED


def _price_the_mismatch(profile: RoleProfile,
                        clean_sheet_rate: float = 0.30) -> None:
    """Points per 90 earned purely from the classification, not the football.

    Compares what this output banks as the player's *listed* position against
    what the identical output would bank if FPL classified them where they
    actually play.
    """
    if not profile.is_arbitrage:
        return
    equivalent = {"DEF": "MID", "MID": "FWD"}.get(profile.position)
    if equivalent is None:
        return

    # Split expected involvement between goals and assists at the usual ~60/40.
    xg90, xa90 = profile.xgi90 * 0.6, profile.xgi90 * 0.4
    actual = (xg90 * GOAL_POINTS[profile.position] + xa90 * ASSIST_POINTS
              + clean_sheet_rate * CLEAN_SHEET_POINTS[profile.position])
    alternative = (xg90 * GOAL_POINTS[equivalent] + xa90 * ASSIST_POINTS
                   + clean_sheet_rate * CLEAN_SHEET_POINTS[equivalent])

    profile.premium_per90 = round(actual - alternative, 2)
    profile.compared_to = equivalent


# --------------------------------------------------------------------------
# Ranked opportunity list
# --------------------------------------------------------------------------
def candidates(conn: sqlite3.Connection, limit: int = 20,
               include_set_pieces: bool = True) -> list[RoleProfile]:
    """Every live role-arbitrage opportunity, best first.

    `include_set_pieces` keeps penalty and free-kick monopolisers in the list
    even when their deployed role matches their listing -- the penalty duty is
    itself a durable points premium, and it is the one signal here that carries
    no inference risk.
    """
    baselines = position_baselines(conn)
    if not baselines:
        return []

    ids = [int(r["player_id"]) for r in conn.execute(
        """SELECT g.player_id, SUM(g.minutes) m
           FROM player_gw g JOIN players p ON p.id = g.player_id
           WHERE g.minutes >= ? AND p.status = 'a'
           GROUP BY g.player_id HAVING m >= ?""",
        (MIN_MINUTES, MIN_MINUTES))]

    out: list[RoleProfile] = []
    for pid in ids:
        profile = role_profile(conn, pid, baselines)
        keep = profile.is_arbitrage or (
            include_set_pieces and (profile.on_penalties or profile.on_freekicks))
        if keep:
            out.append(profile)

    out.sort(key=score, reverse=True)
    return out[:limit]


def score(profile: RoleProfile) -> float:
    """Rank by how much edge is available and how cheaply it can be bought."""
    value = min(profile.attack_ratio, 8.0) * 2.0
    value += profile.premium_per90 * 3.0
    value += 3.0 if profile.on_penalties else 0.0
    value += 1.5 if profile.on_freekicks else 0.0
    value += 0.5 if profile.on_corners else 0.0
    value += max(0.0, 6.0 - profile.cost)          # cheap enablers are worth more
    value -= min(profile.ownership / 10.0, 3.0)    # less edge once widely owned
    return round(value, 2)


def squad_profiles(conn: sqlite3.Connection, gw: int) -> list[RoleProfile]:
    """Role check across the players already in your squad."""
    baselines = position_baselines(conn)
    ids = [int(r["player_id"]) for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = ?", (gw,))]
    return [p for p in (role_profile(conn, pid, baselines) for pid in ids)
            if p.sample]


def badges_for(conn: sqlite3.Connection, player_ids: list[int],
               baselines: dict | None = None) -> dict[int, list[str]]:
    """`{player_id: badges}` for the pitch view and player cards.

    Set-piece badges come from columns on `players`, so they are available even
    for a player with no appearances yet; role badges need minutes and are
    simply absent until there is evidence.
    """
    if not player_ids:
        return {}
    baselines = baselines if baselines is not None else position_baselines(conn)

    out: dict[int, list[str]] = {}
    for pid in player_ids:
        profile = role_profile(conn, pid, baselines)
        if profile.sample:
            out[pid] = profile.badges
            continue
        # No minutes: fall back to the stated set-piece duties alone.
        row = conn.execute(
            """SELECT corners_order, freekicks_order, penalties_order
               FROM players WHERE id = ?""", (pid,)).fetchone()
        if row is None:
            out[pid] = []
            continue
        stub = RoleProfile(
            player_id=pid,
            on_penalties=row["penalties_order"] is not None,
            on_freekicks=(row["freekicks_order"] or 99) <= 2,
            on_corners=row["corners_order"] is not None)
        _apply_set_piece_badges(stub)
        out[pid] = stub.badges
    return out
