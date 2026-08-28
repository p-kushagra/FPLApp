"""Forward fixture planning: gameweek shape, captaincy and chip strategy.

Everything here is deterministic Python over the stored fixture list, the
per-gameweek history in `player_gw` and `config/calendar.yaml`. No AI is involved.

Three ideas drive this module:

1. **A gameweek has a shape.** Most clubs play once, but cup progression and
   European midweeks force postponements, so some clubs play twice (a *double*)
   and some not at all (a *blank*). The FPL API only reveals this weeks later,
   once ties are actually rearranged.
2. **Blanks and doubles can be anticipated.** The cup round dates in
   `config/calendar.yaml` collide with known gameweek windows long before the
   fixture list changes. Projecting that collision is what buys planning time.
3. **Captaincy is expected points per match times the number of matches.** `form`
   and `points_per_game` are already per-match rates, so a double gameweek is a
   multiplier rather than a bonus, and a blank is a hard zero.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from .config import Config
from .db import current_gw

# Captain scoring weights. Kept here so the UI can explain every component.
#
# `ep_next` carries the most weight because it is FPL's own per-match expected
# points and already folds in fixture and history. `form` and `points_per_game` are
# noisy early in a season — one 17-point haul in one appearance makes a bench
# defender look like the best captain alive — so both are shrunk toward a league
# prior by appearance count before they are used.
W_EP = 2.0
W_FORM = 0.8
W_PPG = 0.5
W_FIXTURE = 0.6          # applied to (3 - FDR), so an easy tie adds, a hard one subtracts
W_HOME = 0.3
W_ROTATION = 0.8
H2H_CAP = 2.0            # head-to-head can never swing a captain pick by more than this
H2H_CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3, "none": 0.0}

PRIOR_PPG = 3.0          # roughly what a starting Premier League player returns
PRIOR_APPEARANCES = 5.0  # how many real appearances it takes to outweigh the prior
FULL_MATCH_MINUTES = 85.0  # a captain playing less than this is worth proportionally less

# Captaincy doubles your ceiling, not your average, so it favours positions that can
# explode. A keeper's best realistic week is a fraction of a forward's.
CEILING = {"FWD": 1.15, "MID": 1.10, "DEF": 0.95, "GKP": 0.80}

# A cup round within this many days of a gameweek's fixtures threatens that gameweek.
CUP_COLLISION_DAYS = 4


def _parse_date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _team_shorts(conn: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["short_name"] for r in conn.execute("SELECT id, short_name FROM teams")}


def next_gw(conn: sqlite3.Connection) -> int:
    """The next gameweek still to be played.

    `current_gw` tracks the gameweek FPL is scoring, which stays put until the last
    match finishes. Planning always looks at the one after that.
    """
    row = conn.execute(
        "SELECT MIN(event) gw FROM fixtures WHERE finished = 0 AND event IS NOT NULL"
    ).fetchone()
    return int(row["gw"]) if row and row["gw"] is not None else current_gw(conn)


# ---------------------------------------------------------------------------
# Gameweek shape: blanks and doubles
# ---------------------------------------------------------------------------
def gameweek_shape(conn: sqlite3.Connection, from_gw: int | None = None,
                   horizon: int = 10) -> list[dict]:
    """Per-gameweek fixture counts, with confirmed blanks and doubles.

    Only reports what the fixture list actually says. Anticipated disruption that
    has not reached the API yet comes from `projected_disruption`.
    """
    start = from_gw or next_gw(conn)
    shorts = _team_shorts(conn)
    teams = list(shorts)
    out: list[dict] = []

    for gw in range(start, start + horizon):
        rows = conn.execute(
            """SELECT team_h, team_a, kickoff_time FROM fixtures
               WHERE event = ?""",
            (gw,),
        ).fetchall()
        if not rows:
            continue

        counts = {tid: 0 for tid in teams}
        dates: list[dt.date] = []
        for r in rows:
            counts[r["team_h"]] = counts.get(r["team_h"], 0) + 1
            counts[r["team_a"]] = counts.get(r["team_a"], 0) + 1
            d = _parse_date(r["kickoff_time"])
            if d:
                dates.append(d)

        blanks = sorted(shorts[t] for t, n in counts.items() if n == 0)
        doubles = sorted(shorts[t] for t, n in counts.items() if n >= 2)

        if blanks and doubles:
            kind = "mixed"
        elif doubles:
            kind = "double"
        elif blanks:
            kind = "blank"
        else:
            kind = "normal"

        out.append({
            "gw": gw,
            "fixtures": len(rows),
            "kind": kind,
            "blank_teams": blanks,
            "double_teams": doubles,
            "start_date": min(dates) if dates else None,
            "end_date": max(dates) if dates else None,
            "counts": counts,
        })
    return out


def unscheduled_fixtures(conn: sqlite3.Connection) -> list[dict]:
    """Fixtures the API has stripped of a gameweek — postponed and awaiting a new date.

    These are the raw material of future double gameweeks: every one of them must
    be replayed somewhere, and it lands in a gameweek both clubs already occupy.
    """
    shorts = _team_shorts(conn)
    rows = conn.execute(
        "SELECT id, team_h, team_a FROM fixtures WHERE event IS NULL AND finished = 0"
    ).fetchall()
    return [{
        "fixture_id": r["id"],
        "home": shorts.get(r["team_h"], "?"),
        "away": shorts.get(r["team_a"], "?"),
        "teams": [shorts.get(r["team_h"], "?"), shorts.get(r["team_a"], "?")],
    } for r in rows]


def projected_disruption(conn: sqlite3.Connection, cfg: Config,
                         from_gw: int | None = None,
                         horizon: int = 16) -> list[dict]:
    """Gameweeks at risk of blanking, inferred from the cup calendar.

    The FPL fixture list stays clean until a cup tie is actually scheduled, so a
    gameweek sitting on an FA Cup or EFL Cup round is a blank waiting to happen for
    every club still in that competition. Flagging it here is what makes it
    possible to plan transfers and chips weeks ahead rather than reacting.
    """
    shape = gameweek_shape(conn, from_gw, horizon)
    cup_rounds = (cfg.calendar or {}).get("cup_rounds") or []
    today = dt.date.today()

    out: list[dict] = []
    for gw in shape:
        if not gw["start_date"]:
            continue
        window_open = gw["start_date"] - dt.timedelta(days=CUP_COLLISION_DAYS)
        window_close = (gw["end_date"] or gw["start_date"]) + dt.timedelta(days=CUP_COLLISION_DAYS)

        collisions = []
        for r in cup_rounds:
            date = _parse_date(r.get("date"))
            if date and window_open <= date <= window_close:
                collisions.append({
                    "competition": r.get("competition", "Cup"),
                    "round": r.get("round", ""),
                    "date": date,
                })
        if not collisions:
            continue

        out.append({
            "gw": gw["gw"],
            "start_date": gw["start_date"],
            "already_blank": gw["kind"] in ("blank", "mixed"),
            "collisions": collisions,
            "weeks_notice": max(0, (gw["start_date"] - today).days // 7),
            "reason": " + ".join(
                f"{c['competition']} {c['round']}".strip() for c in collisions),
        })
    return out


# ---------------------------------------------------------------------------
# Fixture runs
# ---------------------------------------------------------------------------
def fixture_run(conn: sqlite3.Connection, team_id: int, from_gw: int | None = None,
                horizon: int = 6) -> dict:
    """Difficulty and volume of a club's next `horizon` gameweeks."""
    start = from_gw or next_gw(conn)
    shorts = _team_shorts(conn)
    rows = conn.execute(
        """SELECT event, team_h, team_a, team_h_difficulty, team_a_difficulty
           FROM fixtures
           WHERE event IS NOT NULL AND event >= ? AND event < ?
             AND (team_h = ? OR team_a = ?)
           ORDER BY event""",
        (start, start + horizon, team_id, team_id),
    ).fetchall()

    fixtures = []
    for r in rows:
        home = r["team_h"] == team_id
        fixtures.append({
            "gw": r["event"],
            "opponent": shorts.get(r["team_a"] if home else r["team_h"], "?"),
            "opponent_id": r["team_a"] if home else r["team_h"],
            "home": home,
            "fdr": (r["team_h_difficulty"] if home else r["team_a_difficulty"]) or 3,
        })

    played_gws = {f["gw"] for f in fixtures}
    blanks = [gw for gw in range(start, start + horizon) if gw not in played_gws]
    seen: dict[int, int] = {}
    for f in fixtures:
        seen[f["gw"]] = seen.get(f["gw"], 0) + 1
    doubles = sorted(gw for gw, n in seen.items() if n >= 2)

    avg_fdr = round(sum(f["fdr"] for f in fixtures) / len(fixtures), 2) if fixtures else None

    if avg_fdr is None:
        label = "— no fixtures"
    elif avg_fdr <= 2.4:
        label = "🟢 Great run"
    elif avg_fdr <= 3.0:
        label = "🟡 Fair run"
    elif avg_fdr <= 3.6:
        label = "🟠 Tough run"
    else:
        label = "🔴 Brutal run"

    return {
        "team": shorts.get(team_id, "?"),
        "fixtures": fixtures,
        "count": len(fixtures),
        "avg_fdr": avg_fdr,
        "home_count": sum(1 for f in fixtures if f["home"]),
        "blank_gws": blanks,
        "double_gws": doubles,
        "label": label,
        "summary": " ".join(
            f"{f['opponent']}({'H' if f['home'] else 'A'},{f['fdr']})" for f in fixtures) or "—",
    }


# ---------------------------------------------------------------------------
# Captaincy
# ---------------------------------------------------------------------------
def _h2h_index(conn: sqlite3.Connection) -> dict[tuple[int, int], dict]:
    """Every player's record against every opponent, in one query.

    Per-player lookups would mean hundreds of round trips per gameweek scanned;
    the whole table is small enough to aggregate once and index in memory.
    """
    from .squad_intel import _confidence

    index: dict[tuple[int, int], dict] = {}
    for r in conn.execute(
        """SELECT player_id, opponent_team, COUNT(*) n, AVG(total_points) avg_pts
           FROM player_gw
           WHERE minutes > 0 AND opponent_team IS NOT NULL
           GROUP BY player_id, opponent_team"""
    ):
        index[(r["player_id"], r["opponent_team"])] = {
            "sample": r["n"],
            "avg_points": round(r["avg_pts"], 2),
            "confidence": _confidence(r["n"]),
        }
    return index


def _shrink(rate: float, appearances: float) -> float:
    """Pull a per-match rate toward the league prior in proportion to how thin it is.

    With one appearance a 17-point haul barely moves off the prior; by ten it is
    trusted almost entirely. This is what stops early-season noise from dominating.
    """
    return ((rate * appearances + PRIOR_PPG * PRIOR_APPEARANCES)
            / (appearances + PRIOR_APPEARANCES))


def _appearance_index(conn: sqlite3.Connection) -> dict[int, dict]:
    """Appearances and average minutes per appearance, for every player at once."""
    return {
        r["player_id"]: {
            "appearances": r["apps"] or 0,
            "avg_minutes": round(r["avg_min"], 1) if r["avg_min"] else 0.0,
        }
        for r in conn.execute(
            """SELECT player_id, COUNT(*) apps, AVG(minutes) avg_min
               FROM player_gw WHERE minutes > 0 GROUP BY player_id"""
        )
    }


def _h2h_adjustment(h2h: dict | None, ppg: float) -> float:
    """How much better or worse a player does against this specific opponent.

    Measured against their own points-per-game, so it captures the matchup rather
    than restating that good players score more. Weighted by sample size and
    capped, because two big hauls are not a forecast.
    """
    if not h2h:
        return 0.0
    delta = h2h["avg_points"] - (ppg or 0.0)
    delta = max(-H2H_CAP, min(H2H_CAP, delta))
    return delta * H2H_CONFIDENCE_WEIGHT.get(h2h["confidence"], 0.0)


def captain_ranking(conn: sqlite3.Connection, cfg: Config | None = None,
                    gw: int | None = None, limit: int = 20,
                    squad_only: bool = False,
                    rotation_cache: dict[int, dict] | None = None,
                    h2h: dict[tuple[int, int], dict] | None = None,
                    appearances: dict[int, dict] | None = None) -> list[dict]:
    """Rank captain options for one gameweek.

    Score = per-match expectation x number of matches that gameweek, minus rotation
    risk. The per-match expectation is a rate, so a double gameweek multiplies it
    rather than adding a bonus, and a blank scores zero.

    `rotation_cache`, `h2h` and `appearances` let a caller scanning many gameweeks
    pay for the expensive lookups once instead of once per gameweek.
    """
    target_gw = gw or next_gw(conn)
    shorts = _team_shorts(conn)
    h2h_index = _h2h_index(conn) if h2h is None else h2h
    apps_index = _appearance_index(conn) if appearances is None else appearances
    rot_cache = {} if rotation_cache is None else rotation_cache

    sql = """SELECT p.*, t.short_name AS team_short
             FROM players p JOIN teams t ON t.id = p.team_id
             WHERE p.status = 'a' AND p.form > 0"""
    params: list = []
    if squad_only:
        sql += " AND p.id IN (SELECT player_id FROM my_picks WHERE gw = ?)"
        params.append(current_gw(conn))
    rows = conn.execute(sql, params).fetchall()

    # One query for the whole gameweek beats one per player.
    fixtures_by_team: dict[int, list[dict]] = {}
    for r in conn.execute(
        """SELECT team_h, team_a, team_h_difficulty, team_a_difficulty
           FROM fixtures WHERE event = ?""",
        (target_gw,),
    ):
        fixtures_by_team.setdefault(r["team_h"], []).append(
            {"opponent_id": r["team_a"], "home": True, "fdr": r["team_h_difficulty"] or 3})
        fixtures_by_team.setdefault(r["team_a"], []).append(
            {"opponent_id": r["team_h"], "home": False, "fdr": r["team_a_difficulty"] or 3})

    scored: list[dict] = []
    for r in rows:
        d = dict(r)
        matches = fixtures_by_team.get(d["team_id"], [])
        stats = apps_index.get(d["id"], {"appearances": 0, "avg_minutes": 0.0})
        apps = float(stats["appearances"])
        ep = d.get("ep_next") or 0.0
        form = _shrink(d.get("form") or 0.0, apps)
        ppg = _shrink(d.get("points_per_game") or 0.0, apps)

        # A captain substituted on the hour is worth less than one who plays 90.
        security = min(1.0, (stats["avg_minutes"] or 0.0) / FULL_MATCH_MINUTES) if apps else 0.6
        ceiling = CEILING.get(d.get("position") or "", 1.0)

        if not matches:
            scored.append({
                **d,
                "cap_score": 0.0, "matches": 0, "opponent": "BLANK",
                "fdr": None, "rotation": "—", "rotation_score": 0.0,
                "h2h_note": "", "per_match": 0.0,
                "components": {"blank": "no fixture this gameweek"},
            })
            continue

        per_match_total = 0.0
        h2h_notes = []
        opponents = []
        for m in matches:
            fixture_bonus = (3 - m["fdr"]) * W_FIXTURE
            home_bonus = W_HOME if m["home"] else 0.0
            record = h2h_index.get((d["id"], m["opponent_id"]))
            if record:
                h2h_notes.append(
                    f"{shorts.get(m['opponent_id'], '?')}: {record['avg_points']}pts "
                    f"avg over {record['sample']}")
            per_match_total += security * ceiling * (
                ep * W_EP + form * W_FORM + ppg * W_PPG + fixture_bonus
                + home_bonus + _h2h_adjustment(record, ppg))
            opponents.append(
                f"{shorts.get(m['opponent_id'], '?')}({'H' if m['home'] else 'A'},{m['fdr']})")

        score = per_match_total
        rotation_band, rotation_score = "—", 0.0
        if cfg is not None:
            from . import congestion
            if d["id"] not in rot_cache:
                rot_cache[d["id"]] = congestion.rotation_risk(conn, cfg, d)
            rot = rot_cache[d["id"]]
            rotation_score = rot["score"]
            rotation_band = rot["band"]
            score -= rotation_score * W_ROTATION

        scored.append({
            **d,
            "cap_score": round(score, 2),
            "per_match": round(per_match_total / len(matches), 2),
            "matches": len(matches),
            "opponent": " + ".join(opponents),
            "fdr": round(sum(m["fdr"] for m in matches) / len(matches), 1),
            "rotation": rotation_band,
            "rotation_score": rotation_score,
            "h2h_note": "; ".join(h2h_notes),
            "security": round(security, 2),
            "appearances": int(apps),
            "components": {
                "expected_points": round(ep * W_EP, 2),
                "form_adjusted": round(form * W_FORM, 2),
                "ppg_adjusted": round(ppg * W_PPG, 2),
                "minutes_security": round(security, 2),
                "position_ceiling": ceiling,
                "fixtures": len(matches),
                "rotation_penalty": round(-rotation_score * W_ROTATION, 2),
            },
        })

    scored.sort(key=lambda x: x["cap_score"], reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------------
# Chip strategy
# ---------------------------------------------------------------------------
def _my_squad_ids(conn: sqlite3.Connection) -> list[int]:
    gw = current_gw(conn)
    return [r["player_id"] for r in conn.execute(
        "SELECT player_id FROM my_picks WHERE gw = ?", (gw,))]


def squad_gameweek_coverage(conn: sqlite3.Connection, from_gw: int | None = None,
                            horizon: int = 10) -> list[dict]:
    """How many fixtures your 15 players collectively have in each gameweek.

    Bench Boost wants the peak, Free Hit wants the trough.
    """
    ids = _my_squad_ids(conn)
    if not ids:
        return []

    shorts = _team_shorts(conn)
    team_of = {r["id"]: r["team_id"] for r in conn.execute(
        f"SELECT id, team_id FROM players WHERE id IN ({','.join('?' * len(ids))})", ids)}

    out = []
    for gw in gameweek_shape(conn, from_gw, horizon):
        counts = gw["counts"]
        per_player = [counts.get(team_of.get(pid), 0) for pid in ids]
        playing = sum(1 for n in per_player if n >= 1)
        out.append({
            "gw": gw["gw"],
            "kind": gw["kind"],
            "total_fixtures": sum(per_player),
            "players_playing": playing,
            "players_blank": len(ids) - playing,
            "players_doubling": sum(1 for n in per_player if n >= 2),
            "blank_teams": [t for t in gw["blank_teams"]
                            if t in {shorts.get(team_of.get(p)) for p in ids}],
        })
    return out


def chip_plan(conn: sqlite3.Connection, cfg: Config | None = None,
              horizon: int = 12) -> dict:
    """Suggest when to play each chip, with the evidence behind each call.

    Deliberately conservative: early in a season the fixture list has no blanks or
    doubles yet, so this reports "hold" rather than inventing a target gameweek.
    """
    coverage = squad_gameweek_coverage(conn, horizon=horizon)
    shape = gameweek_shape(conn, horizon=horizon)
    projected = projected_disruption(conn, cfg, horizon=horizon) if cfg else []
    pending = unscheduled_fixtures(conn)

    plan: dict[str, dict] = {}

    doubles = [g for g in shape if g["double_teams"]]
    blanks = [g for g in shape if g["blank_teams"]]

    # --- Triple Captain: the best single captain score across the horizon -----
    # The rotation and head-to-head lookups are shared across every gameweek in the
    # scan; recomputing them per gameweek is what makes this slow.
    rot_cache: dict[int, dict] = {}
    h2h = _h2h_index(conn)
    apps = _appearance_index(conn)
    tc_best = None
    for g in shape[:horizon]:
        top = captain_ranking(conn, cfg, gw=g["gw"], limit=1, rotation_cache=rot_cache,
                              h2h=h2h, appearances=apps)
        if top and (tc_best is None or top[0]["cap_score"] > tc_best["score"]):
            tc_best = {"gw": g["gw"], "player": top[0]["web_name"],
                       "team": top[0]["team_short"], "score": top[0]["cap_score"],
                       "matches": top[0]["matches"], "opponent": top[0]["opponent"]}
    if tc_best:
        is_dgw = tc_best["matches"] >= 2
        plan["Triple Captain"] = {
            "target_gw": tc_best["gw"] if is_dgw else None,
            "action": "play" if is_dgw else "hold",
            "candidate": f"{tc_best['player']} ({tc_best['team']})",
            "reason": (
                f"GW{tc_best['gw']}: {tc_best['player']} has {tc_best['matches']} "
                f"fixtures ({tc_best['opponent']}), score {tc_best['score']}."
                if is_dgw else
                f"No double gameweek inside GW{shape[0]['gw']}-{shape[-1]['gw']}. "
                f"Best single-fixture option is {tc_best['player']} in "
                f"GW{tc_best['gw']} ({tc_best['opponent']}) — save the chip for a double."
            ),
            "confidence": "medium" if is_dgw else "low",
        }

    # --- Bench Boost: the gameweek where all 15 play the most football --------
    if coverage:
        best = max(coverage, key=lambda c: (c["total_fixtures"], c["players_playing"]))
        worth_it = best["players_doubling"] > 0 or best["players_playing"] == 15
        plan["Bench Boost"] = {
            "target_gw": best["gw"] if best["players_doubling"] else None,
            "action": "play" if best["players_doubling"] else "hold",
            "reason": (
                f"GW{best['gw']}: {best['total_fixtures']} squad fixtures, "
                f"{best['players_doubling']} player(s) doubling, "
                f"{best['players_playing']}/15 playing."
                if worth_it else
                "No gameweek in the horizon gives the bench extra fixtures — "
                "hold until a double gameweek is confirmed."
            ),
            "confidence": "medium" if best["players_doubling"] else "low",
        }

    # --- Free Hit: the gameweek that guts your squad --------------------------
    if coverage:
        worst = min(coverage, key=lambda c: c["players_playing"])
        hurts = worst["players_blank"] >= 4
        plan["Free Hit"] = {
            "target_gw": worst["gw"] if hurts else None,
            "action": "play" if hurts else "hold",
            "reason": (
                f"GW{worst['gw']}: {worst['players_blank']} of your 15 blank "
                f"({', '.join(worst['blank_teams']) or 'unnamed clubs'})."
                if hurts else
                "No gameweek leaves four or more of your squad without a fixture."
            ),
            "confidence": "medium" if hurts else "low",
        }

    # --- Wildcard: rebuild ahead of the worst fixture stretch you own ----------
    wc_reason, wc_gw = "Squad not loaded — refresh **My squad**.", None
    ids = _my_squad_ids(conn)
    if ids:
        team_ids = {r["team_id"] for r in conn.execute(
            f"SELECT DISTINCT team_id FROM players WHERE id IN ({','.join('?' * len(ids))})",
            ids)}
        runs = [fixture_run(conn, tid, horizon=6) for tid in team_ids]
        rough = [r for r in runs if (r["avg_fdr"] or 0) >= 3.5]
        if rough:
            wc_reason = (
                f"{len(rough)} of your clubs face a run averaging FDR 3.5+ over the next "
                f"six gameweeks ({', '.join(sorted(r['team'] for r in rough))}). "
                "Consider wildcarding before it starts.")
            wc_gw = shape[0]["gw"] if shape else None
        else:
            wc_reason = ("No club in your squad faces a punishing six-gameweek run. "
                         "Hold the wildcard for a blank/double swing.")
    plan["Wildcard"] = {
        "target_gw": wc_gw,
        "action": "consider" if wc_gw else "hold",
        "reason": wc_reason,
        "confidence": "low",
    }

    return {
        "plan": plan,
        "confirmed_doubles": [{"gw": g["gw"], "teams": g["double_teams"]} for g in doubles],
        "confirmed_blanks": [{"gw": g["gw"], "teams": g["blank_teams"]} for g in blanks],
        "projected": projected,
        "pending_reschedule": pending,
        "horizon": horizon,
    }


# ---------------------------------------------------------------------------
# Early squad warnings
# ---------------------------------------------------------------------------
def squad_alerts(conn: sqlite3.Connection, cfg: Config | None = None,
                 horizon: int = 8) -> list[dict]:
    """Things worth acting on before they bite, newest deadline first.

    The point is lead time: a blank four gameweeks out is a transfer you can plan,
    not an emergency you react to.
    """
    ids = _my_squad_ids(conn)
    if not ids:
        return []

    placeholders = ",".join("?" * len(ids))
    players = [dict(r) for r in conn.execute(
        f"""SELECT p.id, p.web_name, p.team_id, p.position, p.now_cost,
                   t.short_name AS team_short
            FROM players p JOIN teams t ON t.id = p.team_id
            WHERE p.id IN ({placeholders})""", ids)]

    alerts: list[dict] = []
    runs: dict[int, dict] = {}
    for p in players:
        if p["team_id"] not in runs:
            runs[p["team_id"]] = fixture_run(conn, p["team_id"], horizon=horizon)
        run = runs[p["team_id"]]

        for gw in run["blank_gws"]:
            alerts.append({
                "severity": "high",
                "gw": gw,
                "player": p["web_name"],
                "team": p["team_short"],
                "kind": "Blank gameweek",
                "detail": f"{p['team_short']} has no fixture in GW{gw}.",
            })
        for gw in run["double_gws"]:
            alerts.append({
                "severity": "opportunity",
                "gw": gw,
                "player": p["web_name"],
                "team": p["team_short"],
                "kind": "Double gameweek",
                "detail": f"{p['team_short']} plays twice in GW{gw}.",
            })
        if (run["avg_fdr"] or 0) >= 3.6:
            alerts.append({
                "severity": "medium",
                "gw": run["fixtures"][0]["gw"] if run["fixtures"] else None,
                "player": p["web_name"],
                "team": p["team_short"],
                "kind": "Tough run",
                "detail": f"{p['team_short']} averages FDR {run['avg_fdr']} "
                          f"over the next {horizon} gameweeks: {run['summary']}.",
            })

    for proj in (projected_disruption(conn, cfg, horizon=horizon) if cfg else []):
        alerts.append({
            "severity": "watch",
            "gw": proj["gw"],
            "player": "—",
            "team": "all",
            "kind": "Projected blank risk",
            "detail": f"GW{proj['gw']} collides with {proj['reason']} — clubs still in "
                      f"the competition are likely to have this league fixture postponed "
                      f"({proj['weeks_notice']} week(s) notice).",
        })

    order = {"high": 0, "opportunity": 1, "medium": 2, "watch": 3}
    alerts.sort(key=lambda a: (a["gw"] or 99, order.get(a["severity"], 9)))
    return alerts
