"""One-click tactical briefing: the whole gameweek decision on one page.

Everything here already exists somewhere in the app. The value is assembling it
into a single artefact that can be read in ninety seconds and exported, because
the decision is made once a week under time pressure and flipping between five
screens to reconstruct it is how details get missed.

Composed strictly from other modules' outputs -- no analysis originates here.
That keeps the briefing incapable of disagreeing with the page a number came
from, which would be the worst possible failure for a summary document.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

from ..models import arbitrage as arbitrage_mod
from ..models import minutes as minutes_mod
from ..models import template as template_mod
from ..rules import ELEMENT_TYPE_TO_POS
from ..strategy import captaincy as captaincy_mod


@dataclass
class BriefingPlayer:
    player_id: int
    name: str
    team: str
    position: str
    cost: float
    xp: float = 0.0
    multiplier: float = 1.0
    is_captain: bool = False
    is_vice: bool = False
    bench_order: int = 0
    badges: list[str] = field(default_factory=list)
    flag: str = ""
    opponent: str = ""
    fdr: int | None = None


@dataclass
class Briefing:
    gw: int
    generated_at: str
    starting_xi: list[BriefingPlayer] = field(default_factory=list)
    bench: list[BriefingPlayer] = field(default_factory=list)
    formation: str = ""
    captain: captaincy_mod.CaptainOption | None = None
    captain_reason: str = ""
    regime: captaincy_mod.RegimeCall | None = None
    shield_pick: captaincy_mod.CaptainOption | None = None
    sword_pick: captaincy_mod.CaptainOption | None = None
    hazards: list[dict] = field(default_factory=list)
    differentials: list[template_mod.Differential] = field(default_factory=list)
    template_gaps: list[template_mod.TemplateAsset] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    arbitrage: list[arbitrage_mod.RoleProfile] = field(default_factory=list)
    projected_points: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def has_squad(self) -> bool:
        return bool(self.starting_xi)


def _fixtures(conn: sqlite3.Connection, gw: int) -> dict[int, tuple[int, str]]:
    shorts = {int(r["id"]): r["short_name"] for r in
              conn.execute("SELECT id, short_name FROM teams")}
    out: dict[int, tuple[int, str]] = {}
    for r in conn.execute(
            """SELECT team_h, team_a, team_h_difficulty, team_a_difficulty
               FROM fixtures WHERE event = ?""", (gw,)):
        for team, opp, home in ((r["team_h"], r["team_a"], True),
                                (r["team_a"], r["team_h"], False)):
            if team is None:
                continue
            fdr = int((r["team_h_difficulty"] if home
                       else r["team_a_difficulty"]) or 3)
            venue = "H" if home else "A"
            out[int(team)] = (fdr, f"{shorts.get(opp, '?')} ({venue})")
    return out


def build(conn: sqlite3.Connection, cfg, gw: int, *,
          squad_gw: int | None = None,
          deficit: int = 0, gameweeks_left: int = 10,
          swings: list | None = None) -> Briefing:
    """Assemble the briefing for `gw`.

    `squad_gw` is where the picks come from; it defaults to the newest stored
    squad, which during a live gameweek is the previous one -- FPL does not
    publish next week's picks until you make them.
    """
    now = dt.datetime.now(dt.timezone.utc)
    briefing = Briefing(gw=gw, generated_at=now.isoformat(timespec="seconds"))

    if squad_gw is None:
        row = conn.execute("SELECT MAX(gw) FROM my_picks").fetchone()
        squad_gw = int(row[0]) if row and row[0] else gw

    picks = conn.execute(
        """SELECT mp.player_id, mp.position AS slot, mp.multiplier,
                  mp.is_captain, mp.is_vice, p.web_name, p.element_type,
                  p.now_cost, p.status, p.news, p.news_added, p.team_id,
                  p.chance_of_playing_next_round,
                  t.short_name AS team
           FROM my_picks mp
           JOIN players p ON p.id = mp.player_id
           LEFT JOIN teams t ON t.id = p.team_id
           WHERE mp.gw = ? ORDER BY mp.position""", (squad_gw,)).fetchall()

    if not picks:
        briefing.notes.append(
            "No squad stored. Run 'My squad' on the Refresh page to enable "
            "the personalised sections.")
    else:
        _fill_squad(conn, briefing, picks, gw)

    _fill_captaincy(conn, briefing, gw, deficit, gameweeks_left)
    _fill_market(conn, briefing, gw, [p.player_id for p in
                                      briefing.starting_xi + briefing.bench])
    if swings:
        briefing.hazards = _hazards(swings)

    briefing.alerts = minutes_mod.availability_alerts(conn, squad_gw)
    return briefing


def _fill_squad(conn: sqlite3.Connection, briefing: Briefing,
                picks: list, gw: int) -> None:
    ids = [int(p["player_id"]) for p in picks]
    badges = arbitrage_mod.badges_for(conn, ids)
    fixtures = _fixtures(conn, gw)

    marks = ",".join("?" * len(ids))
    xp = {int(r["player_id"]): float(r["xp_total"] or 0.0) for r in conn.execute(
        f"""SELECT player_id, xp_total FROM xp_projection
            WHERE gw = ? AND player_id IN ({marks})""", [gw, *ids])}

    for pick in picks:
        pid = int(pick["player_id"])
        fdr, opponent = fixtures.get(pick["team_id"], (None, "-"))
        gate = minutes_mod.availability(dict(pick))
        entry = BriefingPlayer(
            player_id=pid, name=pick["web_name"] or "",
            team=pick["team"] or "",
            position=ELEMENT_TYPE_TO_POS.get(pick["element_type"], "MID"),
            cost=float(pick["now_cost"] or 0.0), xp=xp.get(pid, 0.0),
            multiplier=float(pick["multiplier"] or 0),
            is_captain=bool(pick["is_captain"]),
            is_vice=bool(pick["is_vice"]),
            bench_order=max(0, int(pick["slot"] or 0) - 11),
            badges=list(badges.get(pid, [])),
            flag=("OUT" if gate <= 0 else (f"{gate:.0%}" if gate < 1 else "")),
            opponent=opponent, fdr=fdr)
        (briefing.starting_xi if entry.multiplier > 0
         else briefing.bench).append(entry)

    briefing.bench.sort(key=lambda p: p.bench_order)
    counts = {p: 0 for p in ("GKP", "DEF", "MID", "FWD")}
    for player in briefing.starting_xi:
        counts[player.position] = counts.get(player.position, 0) + 1
    briefing.formation = "-".join(str(counts[p]) for p in ("DEF", "MID", "FWD"))
    briefing.projected_points = round(
        sum(p.xp * max(p.multiplier, 1.0) for p in briefing.starting_xi), 1)


def _fill_captaincy(conn: sqlite3.Connection, briefing: Briefing, gw: int,
                    deficit: int, gameweeks_left: int) -> None:
    squad_ids = [p.player_id for p in briefing.starting_xi] or None
    try:
        options = captaincy_mod.matrix(conn, gw, candidate_ids=squad_ids)
    except Exception as exc:                          # never break the briefing
        briefing.notes.append(f"captaincy matrix unavailable: {exc}")
        return
    if not options:
        briefing.notes.append(
            "No captaincy options - run the xP projection for this gameweek.")
        return

    briefing.regime = captaincy_mod.regime(deficit, gameweeks_left)
    briefing.captain, briefing.captain_reason = captaincy_mod.recommend(
        options, briefing.regime)
    briefing.shield_pick = max(options, key=lambda o: o.shield)
    briefing.sword_pick = max(options, key=lambda o: o.sword)


def _fill_market(conn: sqlite3.Connection, briefing: Briefing, gw: int,
                 squad: list[int]) -> None:
    try:
        report = template_mod.build(conn, gw, squad=squad, limit=8)
        briefing.differentials = report.differentials[:5]
        briefing.template_gaps = report.gaps[:5]
        if report.basis_caveat:
            briefing.notes.append(report.basis_caveat)
    except Exception as exc:
        briefing.notes.append(f"template analysis unavailable: {exc}")

    try:
        briefing.arbitrage = [p for p in arbitrage_mod.candidates(conn, limit=5)
                              if p.is_arbitrage][:3]
    except Exception as exc:
        briefing.notes.append(f"role arbitrage unavailable: {exc}")


def _hazards(swings: list) -> list[dict]:
    """Rival-owned assets that can move rank against you, worst first."""
    rows = []
    for swing in swings:
        value = getattr(swing, "swing", 0.0)
        if value >= -0.05:
            continue
        rows.append({
            "player": getattr(swing, "web_name", "") or
                      getattr(swing, "player", ""),
            "ileo": round(getattr(swing, "ileo", 0.0), 2),
            "my_multiplier": getattr(swing, "my_multiplier", 0.0),
            "swing": round(value, 2),
            "exposure": str(getattr(swing, "exposure", "")),
        })
    rows.sort(key=lambda r: r["swing"])
    return rows[:8]


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def to_markdown(briefing: Briefing) -> str:
    """Render the briefing as Markdown, for export or the clipboard."""
    out: list[str] = []
    add = out.append

    add(f"# FPL Tactical Briefing - Gameweek {briefing.gw}")
    add(f"_Generated {briefing.generated_at}Z_")
    add("")

    if briefing.has_squad:
        add(f"## Starting XI ({briefing.formation})")
        add(f"Projected: **{briefing.projected_points} pts**")
        add("")
        add("| Pos | Player | Team | Opponent | FDR | xP | Notes |")
        add("|---|---|---|---|---|---|---|")
        for p in briefing.starting_xi:
            mark = " (C)" if p.is_captain else (" (V)" if p.is_vice else "")
            notes = " ".join(p.badges)
            if p.flag:
                notes = f"**{p.flag}** {notes}".strip()
            add(f"| {p.position} | {p.name}{mark} | {p.team} | {p.opponent} "
                f"| {p.fdr if p.fdr is not None else '-'} | {p.xp:.2f} "
                f"| {notes or '-'} |")
        add("")

        add("## Bench Order")
        for index, p in enumerate(briefing.bench, start=1):
            add(f"{index}. **{p.name}** ({p.position}, {p.team}) - "
                f"{p.opponent}, xP {p.xp:.2f}"
                + (f" - {p.flag}" if p.flag else ""))
        add("")

    if briefing.regime is not None:
        add("## Captaincy - Shield vs Sword")
        add(f"**Regime: {briefing.regime.regime.value.upper()}** - "
            f"{briefing.regime.reason}")
        add("")
        if briefing.captain is not None:
            c = briefing.captain
            add(f"**Recommended: {c.web_name}** ({c.team_short}) - "
                f"xP {c.xp:.2f}, ILEO {c.ileo_cap:.2f}, "
                f"P(haul) {c.p_haul:.0%} - {c.classification}")
            if briefing.captain_reason:
                add(f"> {briefing.captain_reason}")
        if briefing.shield_pick is not None:
            s = briefing.shield_pick
            add(f"- Shield (protect rank): **{s.web_name}** - "
                f"floor {s.p_floor:.0%}, shield score {s.shield:.2f}")
        if briefing.sword_pick is not None:
            w = briefing.sword_pick
            add(f"- Sword (chase rank): **{w.web_name}** - "
                f"P(haul) {w.p_haul:.0%}, sword score {w.sword:.2f}")
        add("")

    if briefing.alerts:
        add("## Availability Alerts")
        for a in briefing.alerts:
            add(f"- **{a['player']}** ({a['team']}) - {a['severity'].upper()}: "
                f"{a['news'] or a['status']} (availability {a['availability']:.0%})")
        add("")

    if briefing.hazards:
        add("## Top Rival Hazards (ILEO)")
        add("| Player | Rival EO | My mult | Swing |")
        add("|---|---|---|---|")
        for h in briefing.hazards:
            add(f"| {h['player']} | {h['ileo']:.2f} | {h['my_multiplier']:.0f} "
                f"| {h['swing']:+.2f} |")
        add("")

    if briefing.template_gaps:
        add("## Template Gaps")
        for a in briefing.template_gaps:
            add(f"- **{a.player}** ({a.team}, {a.cost:.1f}m) - "
                f"{a.ownership:.0f}% owned - {a.risk}")
        add("")

    if briefing.differentials:
        add("## Differential Opportunities")
        add("| Player | Team | Cost | Own | xGI90 | Next 3 |")
        add("|---|---|---|---|---|---|")
        for d in briefing.differentials:
            add(f"| {d.player} | {d.team} | {d.cost:.1f}m | {d.ownership:.1f}% "
                f"| {d.xgi90:.2f} | {d.fixtures} |")
        add("")

    if briefing.arbitrage:
        add("## Role Arbitrage")
        for p in briefing.arbitrage:
            add(f"- **{p.player}** ({p.team}, {p.position}, {p.cost:.1f}m) "
                f"{p.badge_text()} - +{p.premium_per90:.2f} pts/90 from the "
                f"classification alone")
        add("")

    if briefing.notes:
        add("## Caveats")
        for note in briefing.notes:
            add(f"- {note}")

    return "\n".join(out)
