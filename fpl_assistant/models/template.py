"""Template core versus differential punches.

Two opposite jobs, and conflating them is how ranks stall:

* **Template core.** Assets so widely owned in the bracket you are competing in
  that *not* owning one is an active bet against the field. These do not win
  you rank; missing them loses it. Coverage is the metric, not upside.
* **Differential punches.** Low-owned assets with the underlying numbers and the
  fixtures to justify the risk. These are the only things that gain rank once
  your template coverage is complete.

Ownership is measured against the **top-50k sample** (`top_owned`) rather than
the global `selected_by_percent` wherever the sample exists. The distinction is
load-bearing: global ownership is dominated by millions of inactive teams, so a
player can read 8% globally and 35% among the managers you are actually racing.
When no sample has been ingested the module falls back to global ownership and
says so in `basis`, rather than quietly answering a different question.

The differential filter is a conjunction, deliberately:

    ownership < 10%   AND   xGI90 in the top quintile   AND   next-3 FDR <= 2.5

Each clause removes a different failure mode -- a popular pick is not a
differential, a low-owned player without underlying numbers is just bad, and
good underlying numbers into a brutal fixture run will not convert in the window
you can actually hold them for.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Ownership at or above which an asset is "template" for the bracket.
TEMPLATE_OWNERSHIP = 50.0

# Ownership below which an asset is a genuine differential.
DIFFERENTIAL_OWNERSHIP = 10.0

# Underlying-output percentile a differential must clear (top quintile).
DIFFERENTIAL_PERCENTILE = 80.0

# Mean FDR over the next N fixtures a differential must not exceed.
DIFFERENTIAL_MAX_FDR = 2.5
FIXTURE_HORIZON = 3

# Minutes before a per-90 rate is treated as evidence rather than noise.
MIN_MINUTES = 180

BASIS_TOP_SAMPLE = "top_50k_sample"
BASIS_GLOBAL = "global_ownership"


@dataclass
class TemplateAsset:
    """A widely-owned asset and whether you have it covered."""

    player_id: int
    player: str
    team: str
    position: str
    cost: float
    ownership: float          # in the measured bracket
    global_ownership: float
    captaincy: float = 0.0
    owned: bool = False
    xgi90: float = 0.0
    form: float = 0.0
    next_fdr: float = 0.0
    status: str = "a"

    @property
    def risk(self) -> str:
        """What not owning this costs you, in rank terms."""
        if self.owned:
            return "covered"
        if self.ownership >= 70.0:
            return "critical gap"
        return "gap"


@dataclass
class Differential:
    """A low-owned asset that has cleared all three evidence gates."""

    player_id: int
    player: str
    team: str
    position: str
    cost: float
    ownership: float
    global_ownership: float
    xgi90: float
    xgi_percentile: float
    next_fdr: float
    fixtures: str = ""
    minutes: int = 0
    form: float = 0.0
    owned: bool = False
    status: str = "a"
    badges: list[str] = field(default_factory=list)

    @property
    def upside(self) -> float:
        """Rank-gain potential: underlying output weighted by scarcity."""
        scarcity = max(0.0, (DIFFERENTIAL_OWNERSHIP - self.ownership)
                       / DIFFERENTIAL_OWNERSHIP)
        fixture_bonus = max(0.0, (DIFFERENTIAL_MAX_FDR - self.next_fdr) / 2.0)
        return round(self.xgi90 * (1.0 + scarcity + fixture_bonus), 3)


@dataclass
class TemplateReport:
    basis: str = BASIS_GLOBAL
    sample_size: int = 0
    gw: int = 0
    core: list[TemplateAsset] = field(default_factory=list)
    differentials: list[Differential] = field(default_factory=list)
    xgi_threshold: float = 0.0
    funnel: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def binding_gate(self) -> str | None:
        """Which filter emptied the list, for when no differential survives.

        An empty result is a real answer -- early in a season almost nothing
        clears three simultaneous gates -- but it is only useful if the screen
        can say which gate did the work rather than showing a blank table.
        """
        if self.differentials or not self.funnel:
            return None
        stages = [
            ("ownership", "the ownership ceiling"),
            ("minutes", "the minimum-minutes filter"),
            ("xgi", "the top-quintile xGI filter"),
            ("fdr", "the fixture-difficulty filter"),
        ]
        previous = None
        for key, label in stages:
            if self.funnel.get(key, 0) == 0:
                came_from = (f"{self.funnel.get(previous, 0)} reached it"
                             if previous else "the whole player pool")
                return (f"No differentials this week: {label} removed every "
                        f"remaining candidate ({came_from}). Relax that filter "
                        f"to widen the search.")
            previous = key
        return None

    @property
    def coverage(self) -> float:
        """Fraction of the template core you actually own."""
        if not self.core:
            return 1.0
        return round(sum(1 for a in self.core if a.owned) / len(self.core), 3)

    @property
    def gaps(self) -> list[TemplateAsset]:
        return [a for a in self.core if not a.owned]

    @property
    def basis_caveat(self) -> str | None:
        if self.basis == BASIS_TOP_SAMPLE:
            return None
        return ("Ownership is global, not top-50k. Global percentages are "
                "diluted by inactive teams, so template thresholds read low. "
                "Run the top-manager template ingest for bracket-accurate "
                "ownership.")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def _ownership_basis(conn: sqlite3.Connection, gw: int
                     ) -> tuple[dict[int, tuple[float, float]], str, int]:
    """`({player_id: (ownership, captaincy)}, basis, sample_size)`.

    Falls back to the most recent sampled gameweek at or before `gw`. The
    template moves slowly -- last week's top-50k ownership is a far better
    estimate of this week's than global ownership is -- and planning is always
    done for a gameweek that has not been sampled yet, so requiring an exact
    match would mean never using the sample at all.
    """
    rows = conn.execute(
        """SELECT player_id, ownership_pct, captain_pct, sample_size
           FROM top_owned
           WHERE gw = (SELECT MAX(gw) FROM top_owned WHERE gw <= ?)""",
        (gw,)).fetchall()
    if rows:
        sample = max((int(r["sample_size"] or 0) for r in rows), default=0)
        return ({int(r["player_id"]): (float(r["ownership_pct"] or 0.0),
                                       float(r["captain_pct"] or 0.0))
                 for r in rows}, BASIS_TOP_SAMPLE, sample)
    return {}, BASIS_GLOBAL, 0


def _xgi_rates(conn: sqlite3.Connection) -> dict[int, tuple[float, int]]:
    """`{player_id: (xGI90, minutes)}` for players with a usable sample."""
    out: dict[int, tuple[float, int]] = {}
    for r in conn.execute(
            """SELECT player_id, SUM(minutes) mins,
                      SUM(expected_goal_involvements) xgi
               FROM player_gw GROUP BY player_id HAVING mins >= ?""",
            (MIN_MINUTES,)):
        mins = float(r["mins"] or 0)
        if mins > 0:
            out[int(r["player_id"])] = (90.0 * float(r["xgi"] or 0.0) / mins,
                                        int(mins))
    return out


def percentile_threshold(values: list[float], percentile: float) -> float:
    """Value at `percentile` using linear interpolation between ranks."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def percentile_of(values: list[float], value: float) -> float:
    """Where `value` sits in `values`, as a 0-100 percentile."""
    if not values:
        return 0.0
    below = sum(1 for v in values if v < value)
    return round(100.0 * below / len(values), 1)


def fixture_outlook(conn: sqlite3.Connection, team_id: int, from_gw: int,
                    horizon: int = FIXTURE_HORIZON) -> tuple[float, str]:
    """`(mean FDR, "ARS(H,2) CHE(A,4)")` over the next `horizon` fixtures.

    A blank counts as the worst possible difficulty rather than being skipped:
    a player with two fixtures in the next three is objectively worse placed
    than one with three, and averaging only the fixtures that exist would hide
    exactly that.
    """
    shorts = {int(r["id"]): r["short_name"] for r in
              conn.execute("SELECT id, short_name FROM teams")}
    rows = conn.execute(
        """SELECT event, team_h, team_a, team_h_difficulty, team_a_difficulty
           FROM fixtures
           WHERE event IS NOT NULL AND event >= ? AND event < ?
             AND (team_h = ? OR team_a = ?)
           ORDER BY event""",
        (from_gw, from_gw + horizon, team_id, team_id)).fetchall()

    difficulties: list[float] = []
    parts: list[str] = []
    for r in rows:
        home = r["team_h"] == team_id
        opp = shorts.get(r["team_a"] if home else r["team_h"], "?")
        fdr = float((r["team_h_difficulty"] if home
                     else r["team_a_difficulty"]) or 3)
        difficulties.append(fdr)
        parts.append(f"{opp}({'H' if home else 'A'},{int(fdr)})")

    blanks = horizon - len(difficulties)
    difficulties.extend([5.0] * max(0, blanks))
    if blanks > 0:
        parts.append(f"{blanks} blank" + ("s" if blanks > 1 else ""))

    mean = sum(difficulties) / len(difficulties) if difficulties else 5.0
    return round(mean, 2), " ".join(parts)


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------
def build(conn: sqlite3.Connection, gw: int, *,
          squad: list[int] | None = None,
          template_threshold: float = TEMPLATE_OWNERSHIP,
          differential_threshold: float = DIFFERENTIAL_OWNERSHIP,
          max_fdr: float = DIFFERENTIAL_MAX_FDR,
          horizon: int = FIXTURE_HORIZON,
          limit: int = 25) -> TemplateReport:
    """Classify the whole player pool into template core and differentials."""
    sample_own, basis, sample_size = _ownership_basis(conn, gw)
    rates = _xgi_rates(conn)
    owned = set(squad or [])

    report = TemplateReport(basis=basis, sample_size=sample_size, gw=gw)

    players = conn.execute(
        """SELECT p.id, p.web_name, p.position, p.element_type, p.now_cost,
                  p.selected_by_percent, p.form, p.status, p.team_id,
                  p.penalties_order, p.corners_order, p.freekicks_order,
                  t.short_name AS team
           FROM players p LEFT JOIN teams t ON t.id = p.team_id""").fetchall()
    if not players:
        report.notes.append("no players ingested")
        return report

    # The percentile is computed over players with a real sample only. Including
    # 250 zero-minute players would drag the top quintile down to a rate any
    # bench player clears.
    pool = [rate for rate, _mins in rates.values()]
    report.xgi_threshold = round(
        percentile_threshold(pool, DIFFERENTIAL_PERCENTILE), 3)

    # The next unplayed gameweek is the planning anchor for fixtures.
    anchor_row = conn.execute(
        "SELECT MIN(event) FROM fixtures WHERE finished = 0 AND event IS NOT NULL"
    ).fetchone()
    anchor = int(anchor_row[0]) if anchor_row and anchor_row[0] else gw + 1
    fixture_cache: dict[int, tuple[float, str]] = {}

    def outlook(team_id) -> tuple[float, str]:
        if team_id is None:
            return 5.0, "-"
        if team_id not in fixture_cache:
            fixture_cache[team_id] = fixture_outlook(conn, int(team_id),
                                                     anchor, horizon)
        return fixture_cache[team_id]

    core: list[TemplateAsset] = []
    diffs: list[Differential] = []
    funnel = dict.fromkeys(("ownership", "minutes", "xgi", "fdr"), 0)

    for p in players:
        pid = int(p["id"])
        global_own = float(p["selected_by_percent"] or 0.0)
        bracket_own, captaincy = sample_own.get(pid, (global_own, 0.0))
        rate, minutes = rates.get(pid, (0.0, 0))

        if bracket_own >= template_threshold:
            fdr, _fx = outlook(p["team_id"])
            core.append(TemplateAsset(
                player_id=pid, player=p["web_name"] or "", team=p["team"] or "",
                position=p["position"] or "", cost=float(p["now_cost"] or 0.0),
                ownership=round(bracket_own, 1), global_ownership=global_own,
                captaincy=round(captaincy, 1), owned=pid in owned,
                xgi90=round(rate, 3), form=float(p["form"] or 0.0),
                next_fdr=fdr, status=p["status"] or "a"))
            continue

        # Differential gates, in cheapest-to-evaluate order. Each survivor
        # count is recorded so an empty result can name the binding gate.
        if bracket_own >= differential_threshold:
            continue
        funnel["ownership"] += 1
        if minutes < MIN_MINUTES:
            continue
        funnel["minutes"] += 1
        if rate < report.xgi_threshold or report.xgi_threshold <= 0:
            continue
        funnel["xgi"] += 1
        if (p["status"] or "a") != "a":
            continue
        fdr, fixtures = outlook(p["team_id"])
        if fdr > max_fdr:
            continue
        funnel["fdr"] += 1

        badges = []
        if p["penalties_order"] is not None:
            badges.append("[Penalties]")
        if (p["freekicks_order"] or 99) <= 2:
            badges.append("[Free Kicks]")
        if p["corners_order"] is not None:
            badges.append("[Corners]")

        diffs.append(Differential(
            player_id=pid, player=p["web_name"] or "", team=p["team"] or "",
            position=p["position"] or "", cost=float(p["now_cost"] or 0.0),
            ownership=round(bracket_own, 1), global_ownership=global_own,
            xgi90=round(rate, 3),
            xgi_percentile=percentile_of(pool, rate),
            next_fdr=fdr, fixtures=fixtures, minutes=minutes,
            form=float(p["form"] or 0.0), owned=pid in owned,
            status=p["status"] or "a", badges=badges))

    core.sort(key=lambda a: a.ownership, reverse=True)
    diffs.sort(key=lambda d: d.upside, reverse=True)
    report.core = core[:limit]
    report.differentials = diffs[:limit]
    report.funnel = funnel
    if not diffs and report.binding_gate:
        report.notes.append(report.binding_gate)

    if not core:
        report.notes.append(
            f"no asset reaches {template_threshold:.0f}% ownership - "
            "either the season is young or the sample is global")
    return report
