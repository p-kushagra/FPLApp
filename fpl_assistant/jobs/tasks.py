"""The job catalogue.

Every task is a plain function taking `conn` and `progress`, so both runners
execute identical code -- switching to Celery changes submission only.

Tasks own the ingest side-effects; pure computation stays in `models/` and
`strategy/`. That split is what lets a projection be recomputed without a
network round trip.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .. import leagues as leagues_mod
from .. import temporal
from ..models import calibration
from ..models import snapshot as snapshot_mod
from ..models import xp as xp_model
from ..resolve import aliases as alias_mod
from ..resolve import matcher
from ..sources.base import Quality
from ..sources.fpl import FplSource
from ..sources.understat import UnderstatSource
from ..strategy import eo as eo_mod

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]

UNDERSTAT_CHUNK = 25   # players per fan-out job; isolates failures
SEASON_FALLBACK = 2025

# Courtesy pause between per-player Understat fetches, on top of the token
# bucket. Enrichment is never on anyone's critical path.
PLAYER_FETCH_PAUSE = 0.5


def _noop(progress: float, note: str = "") -> None:
    return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# DAG-A: reference refresh
# --------------------------------------------------------------------------
def refresh_reference(conn: sqlite3.Connection, progress: Progress = _noop,
                      **_: Any) -> dict:
    """bootstrap-static + fixtures -> teams, players, gw_state, price snapshot."""
    src = FplSource(conn)
    progress(0.1, "fetching bootstrap")

    boot = src.bootstrap()
    if not boot.usable:
        return {"ok": False, "reason": "bootstrap unavailable", "quality": boot.quality}

    data = boot.data or {}
    progress(0.4, "writing gameweek state")
    events = data.get("events") or []
    written = temporal.sync_gw_state(conn, events)

    progress(0.6, "snapshotting prices")
    snapshots = snapshot_prices(conn, elements=data.get("elements"))

    progress(0.9, "fetching fixtures")
    fixtures = src.fixtures()

    return {
        "ok": True,
        "gameweeks": written,
        "price_snapshots": snapshots.get("snapshots", 0),
        "price_changes": snapshots.get("changes", 0),
        "fixtures_quality": fixtures.quality.value,
        "bootstrap_quality": boot.quality.value,
    }


def snapshot_prices(conn: sqlite3.Connection, progress: Progress = _noop,
                    elements: list[dict] | None = None, **_: Any) -> dict:
    """Append a price/transfer-flow snapshot and detect changes since the last.

    v1 stored only the CURRENT transfer counts, so momentum -- the flow rate,
    which is what actually drives a price change -- was unrecoverable. This is
    the time series that makes the price model possible at all.
    """
    if elements is None:
        src = FplSource(conn)
        boot = src.bootstrap()
        if not boot.usable:
            return {"ok": False, "snapshots": 0, "changes": 0}
        elements = (boot.data or {}).get("elements") or []

    now = _now()
    previous = {
        r["player_id"]: float(r["now_cost"])
        for r in conn.execute(
            """SELECT player_id, now_cost FROM price_snapshot
               WHERE captured_at = (SELECT MAX(captured_at) FROM price_snapshot)"""
        )
    }

    rows, changes = [], 0
    for e in elements:
        pid = e["id"]
        cost = float(e["now_cost"]) / 10.0
        tin = int(e.get("transfers_in_event") or 0)
        tout = int(e.get("transfers_out_event") or 0)
        rows.append((pid, now, cost, float(e.get("selected_by_percent") or 0),
                     tin, tout, tin - tout))

        old = previous.get(pid)
        if old is not None and abs(old - cost) >= 0.05:
            conn.execute(
                """INSERT OR REPLACE INTO price_change
                     (player_id, changed_at, old_cost, new_cost, direction)
                   VALUES (?, ?, ?, ?, ?)""",
                (pid, now, old, cost, 1 if cost > old else -1),
            )
            changes += 1

    conn.executemany(
        """INSERT OR REPLACE INTO price_snapshot
             (player_id, captured_at, now_cost, selected_by_percent,
              transfers_in_event, transfers_out_event, net_transfers)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return {"ok": True, "snapshots": len(rows), "changes": changes}


# --------------------------------------------------------------------------
# DAG-B: Understat enrichment
# --------------------------------------------------------------------------
def ingest_understat_league(conn: sqlite3.Connection, progress: Progress = _noop,
                            season: int = SEASON_FALLBACK,
                            enabled: bool = True, **_: Any) -> dict:
    """Season aggregates for every player in ONE request.

    Always attempted before any per-player fan-out: 700 player pages versus one
    league page is the difference between a 45-minute cold start and a 3-second
    one.
    """
    src = UnderstatSource(conn, enabled=enabled)
    progress(0.2, "fetching Understat league page")

    players = src.league_players(season)
    if not players.usable:
        _flag_understat_offline(conn, players.error or "league page unavailable")
        return {"ok": False, "degraded": True, "quality": players.quality.value,
                "reason": players.error}

    now = _now()
    rows = []
    for p in players.data or []:
        rows.append((
            str(p.get("id")), season, p.get("player_name"), p.get("team_title"),
            p.get("position"), _int(p.get("games")), _int(p.get("time")),
            _int(p.get("goals")), _int(p.get("assists")), _int(p.get("shots")),
            _int(p.get("key_passes")), _f(p.get("xG")), _f(p.get("xA")),
            _int(p.get("npg")), _f(p.get("npxG")), _f(p.get("xGChain")),
            _f(p.get("xGBuildup")), _int(p.get("yellow_cards")),
            _int(p.get("red_cards")), now,
        ))

    conn.executemany(
        """INSERT OR REPLACE INTO understat_player
             (understat_id, season, player_name, team_title, position, games,
              time_min, goals, assists, shots, key_passes, xg, xa, npg, npxg,
              xg_chain, xg_buildup, yellow_cards, red_cards, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )

    progress(0.7, "fetching team rates")
    teams = src.league_teams(season)
    team_rows = 0
    if teams.usable:
        payload = teams.data
        entries = payload.values() if isinstance(payload, dict) else payload
        for t in entries or []:
            history = t.get("history") or []
            games = len(history)
            conn.execute(
                """INSERT OR REPLACE INTO understat_team
                     (team_title, season, games, xg, xga, npxg, npxga,
                      deep, deep_allowed, ppda, ppda_allowed, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (t.get("title"), season, games,
                 sum(_f(h.get("xG")) for h in history),
                 sum(_f(h.get("xGA")) for h in history),
                 sum(_f(h.get("npxG")) for h in history),
                 sum(_f(h.get("npxGA")) for h in history),
                 sum(_int(h.get("deep")) for h in history),
                 sum(_int(h.get("deep_allowed")) for h in history),
                 0.0, 0.0, now),
            )
            team_rows += 1

    conn.commit()
    _flag_understat_online(conn)
    return {"ok": True, "players": len(rows), "teams": team_rows,
            "quality": players.quality.value}


def understat_fanout(conn: sqlite3.Connection, progress: Progress = _noop,
                     understat_ids: list[str] | None = None,
                     enabled: bool = True, **_: Any) -> dict:
    """Per-match rows for a chunk of players.

    One player's failure marks that player baseline and continues; it never
    fails the batch. Three consecutive BATCH failures flip source_health to
    down and the global degradation badge appears.
    """
    src = UnderstatSource(conn, enabled=enabled)
    ids = understat_ids or []
    if not ids:
        return {"ok": True, "players": 0, "matches": 0}

    fixtures = _fixture_date_index(conn)
    total_matches = 0
    total_shots = 0
    failures: list[str] = []
    now = _now()

    for i, uid in enumerate(ids):
        progress(i / max(1, len(ids)), f"understat player {uid}")

        # Courtesy gap between players, on top of the token bucket. Understat
        # is a small volunteer-run site and this is enrichment, not something
        # anyone is waiting on.
        if i:
            time.sleep(PLAYER_FETCH_PAUSE)

        # One fetch serves both: `matches` has no home/away flag of its own, so
        # the player's club per season -- which only `groups` carries -- is what
        # makes the side derivable at all.
        payload = src.player_data(str(uid))
        if not payload.usable:
            failures.append(str(uid))
            continue

        matches = (payload.data or {}).get("matches") or []
        teams_by_season = _teams_by_season((payload.data or {}).get("groups"))
        total_shots += _store_shots(conn, str(uid),
                                    (payload.data or {}).get("shots"), now)

        for m in matches:
            gw = _match_to_gw(conn, m, fixtures)
            home, away = m.get("h_team"), m.get("a_team")
            mine = teams_by_season.get(_int(m.get("season")), set())

            # Which side was this player on? Whichever club is theirs that
            # season. A transfer inside one season leaves both in the set, so
            # an unresolvable match records NULL rather than a coin flip.
            if home in mine and away not in mine:
                is_home, team, opponent = 1, home, away
            elif away in mine and home not in mine:
                is_home, team, opponent = 0, away, home
            else:
                is_home, team, opponent = None, None, None

            conn.execute(
                """INSERT OR REPLACE INTO understat_player_match
                     (understat_id, match_id, season, match_date, team_title,
                      opponent_title, is_home, minutes, position, goals, assists,
                      shots, key_passes, xg, xa, npg, npxg, xg_chain, xg_buildup,
                      fpl_gw, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uid), str(m.get("id")), _int(m.get("season")), m.get("date"),
                 team, opponent, is_home,
                 _int(m.get("time")), m.get("position"), _int(m.get("goals")),
                 _int(m.get("assists")), _int(m.get("shots")),
                 _int(m.get("key_passes")), _f(m.get("xG")), _f(m.get("xA")),
                 _int(m.get("npg")), _f(m.get("npxG")), _f(m.get("xGChain")),
                 _f(m.get("xGBuildup")), gw, now),
            )
            total_matches += 1

    conn.commit()
    if failures and len(failures) == len(ids):
        _flag_understat_offline(conn, f"all {len(ids)} players failed")

    return {"ok": True, "players": len(ids) - len(failures),
            "matches": total_matches, "shots": total_shots,
            "failed": failures}


def resolve_entities(conn: sqlite3.Connection, progress: Progress = _noop,
                     season: int = SEASON_FALLBACK, **_: Any) -> dict:
    """Bind FPL players to Understat entities and persist to `entity_map`."""
    progress(0.2, "loading aliases")
    team_aliases = alias_mod.team_aliases()
    overrides = alias_mod.overrides()

    progress(0.5, "resolving")
    report = matcher.resolve_all(conn, season, team_aliases, overrides)

    return {
        "ok": True,
        "total": report.total,
        "resolved": report.resolved,
        "unresolved": report.unresolved,
        "conflicts": len(report.conflicts),
        "rate": round(report.resolution_rate, 4),
        "by_method": report.by_method(),
    }


def recompute_xp(conn: sqlite3.Connection, progress: Progress = _noop,
                 gws: list[int] | None = None, horizon: int = 5,
                 understat_ok: bool | None = None, **_: Any) -> dict:
    """Project the planning window. Pure compute; no network."""
    window = gws or temporal.planning_window(conn, horizon)
    if understat_ok is None:
        understat_ok = not understat_offline(conn)

    progress(0.3, f"projecting GW{window[0]}-{window[-1]}")
    results = xp_model.project(conn, window, understat_ok=understat_ok)

    sources: dict[str, int] = {}
    for bd in results.values():
        sources[bd.source] = sources.get(bd.source, 0) + 1

    return {"ok": True, "gws": window, "projections": len(results),
            "sources": sources, "understat_ok": understat_ok}


def freeze_projections(conn: sqlite3.Connection, progress: Progress = _noop,
                       gws: list[int] | None = None, force: bool = False,
                       lookahead: int = 3, **_: Any) -> dict:
    """Freeze pre-deadline xP for any gameweek inside the capture window.

    Scheduled alongside the other pre-deadline jobs and safe to run on every
    tick: `snapshot.capture` refuses a gameweek that is already frozen, too far
    out, or past its deadline, and reports the refusal instead of raising. That
    makes the correct cron for this "run it often" rather than "run it once at
    exactly the right minute", which is the only shape that survives a laptop
    being closed at the wrong moment.
    """
    if gws is None:
        candidates = [c.gw for c in snapshot_mod.due(conn, lookahead=lookahead)]
    else:
        candidates = list(gws)

    if not candidates:
        return {"ok": True, "frozen": [], "skipped": [],
                "note": "no gameweek inside the capture window"}

    understat_ok = not understat_offline(conn)
    frozen: list[dict] = []
    skipped: list[dict] = []

    for i, gw in enumerate(candidates, start=1):
        progress(i / len(candidates), f"freezing GW{gw}")
        result = snapshot_mod.capture(conn, gw, force=force,
                                      understat_ok=understat_ok)
        record = {"gw": gw, "rows": result.rows, "reason": result.reason,
                  "deadline_source": result.deadline_source,
                  "lead_minutes": (round(result.lead_minutes, 1)
                                   if result.lead_minutes is not None else None)}
        (frozen if result.frozen else skipped).append(record)

    return {"ok": True, "frozen": frozen, "skipped": skipped,
            "understat_ok": understat_ok}


def calibrate(conn: sqlite3.Connection, progress: Progress = _noop,
              gws: list[int] | None = None, fit: bool = False,
              **_: Any) -> dict:
    """Score the model against realised points and record the verdict.

    Read-only with respect to projections -- it never rewrites a forecast, only
    grades it -- so it is safe to run at any point in the gameweek cycle.
    """
    progress(0.2, "scoring projections")
    report = calibration.evaluate(conn, gws)
    fits = calibration.fit_affine(conn, report) if fit else []
    progress(0.9, "recording verdict")
    calibration.persist(conn, report)

    ref = report.reference
    return {"ok": True, "verdict": report.verdict, "run_id": report.run_id,
            "gws": report.gws, "n_rows": report.n_rows,
            "rmse_model": report.rmse,
            "baseline": ref.name if ref else None,
            "rmse_baseline": ref.rmse if ref else None,
            "decile_monotonic": report.decile.monotonic,
            "decile_spearman": report.decile.spearman,
            "blockers": report.blockers,
            "fits": len(fits)}


# --------------------------------------------------------------------------
# DAG-C: mini-league freeze
# --------------------------------------------------------------------------
def discover_leagues(conn: sqlite3.Connection, progress: Progress = _noop,
                     team_id: int = 0, **_: Any) -> dict:
    """Read the manager's own mini-league memberships from `/entry/{id}/`.

    This is the step that was missing: every other league table and every ILEO
    surface was already built, but no code path ever produced a league id, so
    they all rendered their empty state permanently.
    """
    if not team_id:
        from ..config import load_config
        team_id = load_config().fpl_team_id or 0

    progress(0.3, "reading league memberships")
    return leagues_mod.discover(conn, int(team_id))


def ingest_mini_league(conn: sqlite3.Connection, progress: Progress = _noop,
                       league_id: int = 0, limit: int = 50, **_: Any) -> dict:
    """Standings for one classic league, or every tracked league.

    A league_id of 0 means "whatever the user is tracking", which is what lets
    the daemon run this on a schedule without anything hard-coded.
    """
    targets = [int(league_id)] if league_id else leagues_mod.tracked_ids(conn)
    if not targets:
        return {"ok": False, "reason": "no tracked leagues", "entries": 0,
                "leagues": 0}

    src = FplSource(conn)
    gw = temporal.gw_state(conn).scoring_gw
    now = _now()
    total = 0
    partial = False
    done: list[int] = []

    for i, lid in enumerate(targets):
        progress(i / max(1, len(targets)), f"fetching league {lid}")
        result = src.league_entries(lid, limit=limit)
        if not result.usable:
            continue
        rows = result.data or []
        for row in rows:
            conn.execute(
                """INSERT OR REPLACE INTO league_standing
                     (league_id, gw, entry_id, player_name, entry_name, rank,
                      last_rank, event_total, total, is_rival, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE((SELECT is_rival FROM league_standing
                                     WHERE league_id = ? AND entry_id = ?
                                     ORDER BY gw DESC LIMIT 1), 0), ?)""",
                (lid, gw, row.get("entry"), row.get("player_name"),
                 row.get("entry_name"), row.get("rank"), row.get("last_rank"),
                 row.get("event_total"), row.get("total"),
                 lid, row.get("entry"), now),
            )
        # Size is what distinguishes a work league from a public free-for-all;
        # it decides which league the rival-facing pages default to.
        conn.execute(
            """INSERT INTO league (league_id, entry_count, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(league_id) DO UPDATE SET
                 entry_count = excluded.entry_count,
                 updated_at = excluded.updated_at""",
            (lid, len(rows), now))
        total += len(rows)
        partial = partial or result.quality.is_degraded
        done.append(lid)

    conn.commit()
    return {"ok": bool(done), "entries": total, "leagues": len(done),
            "league_ids": done, "partial": partial, "gw": gw}


def freeze_rivals(conn: sqlite3.Connection, progress: Progress = _noop,
                  league_id: int = 0, gw: int = 0,
                  rival_ids: list[int] | None = None, **_: Any) -> dict:
    """Snapshot rival squads once, after the deadline (ADR-005).

    Idempotent: an entry already frozen for this gameweek is skipped, so a
    duplicate trigger costs nothing. A partial fetch is kept rather than rolled
    back -- eight of twelve rivals still yields a usable ILEO with an adjusted
    denominator, which is what the degradation matrix asks for.
    """
    state = temporal.gw_state(conn)
    target_gw = gw or state.scoring_gw

    if not state.rivals_frozen:
        return {"ok": False, "reason": "deadline has not passed", "frozen": 0,
                "phase": state.phase.value}

    if rival_ids:
        rivals = [int(r) for r in rival_ids]
    elif league_id:
        # A tracked league with no curated rival set still gets a usable
        # default rather than an empty ILEO nobody knows how to populate.
        rivals = leagues_mod.ensure_rivals(
            conn, league_id, exclude_entry=_my_entry_id())
    else:
        rivals = []
        for lid in leagues_mod.tracked_ids(conn):
            rivals.extend(leagues_mod.ensure_rivals(
                conn, lid, exclude_entry=_my_entry_id()))
        rivals = sorted(set(rivals))

    if not rivals:
        return {"ok": False, "reason": "no rivals selected", "frozen": 0}

    src = FplSource(conn)
    now = _now()
    frozen = skipped = failed = 0

    for i, entry_id in enumerate(rivals):
        progress(i / max(1, len(rivals)), f"rival {entry_id}")

        already = conn.execute(
            """SELECT 1 FROM league_rival_pick
               WHERE entry_id = ? AND gw = ? AND frozen = 1 LIMIT 1""",
            (entry_id, target_gw),
        ).fetchone()
        if already:
            skipped += 1
            continue

        result = src.picks(entry_id, target_gw, frozen=True)
        if not result.usable:
            failed += 1
            continue

        payload = result.data or {}
        chip = payload.get("active_chip")
        for pick in payload.get("picks") or []:
            conn.execute(
                """INSERT OR REPLACE INTO league_rival_pick
                     (entry_id, gw, player_id, position, multiplier,
                      is_captain, is_vice, chip, frozen, frozen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (entry_id, target_gw, pick.get("element"), pick.get("position"),
                 pick.get("multiplier"), 1 if pick.get("is_captain") else 0,
                 1 if pick.get("is_vice_captain") else 0, chip, now),
            )
        frozen += 1

    conn.commit()

    # `ileo_cache` keys on league_id, so a fan-out across every tracked league
    # is attributed to the primary one rather than to a meaningless 0.
    attributed = league_id or leagues_mod.default_league(conn) or 0
    matrix = eo_mod.swing_matrix(conn, target_gw, rivals, league_id=attributed)
    written = eo_mod.persist_ileo(conn, matrix)

    return {"ok": True, "gw": target_gw, "frozen": frozen, "skipped": skipped,
            "failed": failed, "requested": len(rivals),
            "ileo_rows": written, "partial": matrix.partial}


# --------------------------------------------------------------------------
# DAG-D: live polling
# --------------------------------------------------------------------------
def poll_live(conn: sqlite3.Connection, progress: Progress = _noop,
              gw: int = 0, **_: Any) -> dict:
    """Refresh live scoring. 60s tier; serves cache on failure."""
    state = temporal.gw_state(conn)
    target = gw or state.scoring_gw
    src = FplSource(conn)
    result = src.live(target)
    if not result.usable:
        return {"ok": False, "gw": target, "quality": result.quality.value}

    elements = (result.data or {}).get("elements") or []
    return {"ok": True, "gw": target, "players": len(elements),
            "quality": result.quality.value,
            "stale": result.quality is not Quality.FRESH}


# --------------------------------------------------------------------------
# Degradation flags
# --------------------------------------------------------------------------
def _flag_understat_offline(conn: sqlite3.Connection, reason: str) -> None:
    conn.execute(
        """INSERT INTO source_health (source, last_failure_at, last_error,
                                      consecutive_failures, quality, updated_at)
           VALUES ('understat', ?, ?, 1, 'down', ?)
           ON CONFLICT(source) DO UPDATE SET
             last_failure_at = excluded.last_failure_at,
             last_error = excluded.last_error,
             consecutive_failures = source_health.consecutive_failures + 1,
             quality = 'down', updated_at = excluded.updated_at""",
        (_now(), str(reason)[:500], _now()),
    )
    conn.commit()


def _flag_understat_online(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO source_health (source, last_success_at,
                                      consecutive_failures, quality, updated_at)
           VALUES ('understat', ?, 0, 'ok', ?)
           ON CONFLICT(source) DO UPDATE SET
             last_success_at = excluded.last_success_at,
             consecutive_failures = 0, quality = 'ok',
             updated_at = excluded.updated_at""",
        (_now(), _now()),
    )
    conn.commit()


def understat_offline(conn: sqlite3.Connection) -> bool:
    """True when the UI owes the operator the offline badge."""
    row = conn.execute(
        "SELECT quality FROM source_health WHERE source = 'understat'"
    ).fetchone()
    return bool(row and row["quality"] == "down")


def degradation_state(conn: sqlite3.Connection) -> dict:
    """Everything the UI needs to render its badges."""
    rows = {r["source"]: dict(r) for r in conn.execute("SELECT * FROM source_health")}
    understat = rows.get("understat", {})
    offline = understat.get("quality") == "down"
    return {
        "understat_offline": offline,
        "understat_badge": ("Understat Offline - Using Baseline Stats"
                            if offline else None),
        "sources": {k: v.get("quality") for k, v in rows.items()},
        "last_error": understat.get("last_error"),
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _my_entry_id() -> int | None:
    """Own team id, so auto-selection never makes you your own rival."""
    try:
        from ..config import load_config
        return load_config().fpl_team_id
    except Exception:
        return None


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _store_shots(conn: sqlite3.Connection, understat_id: str, shots: Any,
                 now: str) -> int:
    """Persist a player's shot events. Rides along on the fan-out's payload.

    Understat returns every shot of a player's career, not just this season, so
    the rows are keyed on the shot id and replaced rather than appended -- a
    re-ingest must not double a striker's shot map.
    """
    rows = []
    for s in shots or []:
        shot_id = s.get("id")
        if shot_id is None:
            continue
        rows.append((
            str(shot_id), understat_id, str(s.get("match_id") or ""),
            _int(s.get("season")), _int(s.get("minute")),
            _f(s.get("X")), _f(s.get("Y")), _f(s.get("xG")),
            s.get("result"), s.get("situation"), s.get("shotType"),
            s.get("lastAction"), s.get("h_team"), s.get("a_team"),
            s.get("h_a"), s.get("player_assisted"), s.get("date"), now,
        ))

    if rows:
        conn.executemany(
            """INSERT OR REPLACE INTO understat_shot
                 (shot_id, understat_id, match_id, season, minute, x, y, xg,
                  result, situation, shot_type, last_action, h_team, a_team,
                  h_a, player_assisted, match_date, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows)
    return len(rows)


def _teams_by_season(groups: Any) -> dict[int, set[str]]:
    """{season: {clubs the player turned out for}} from the `groups` payload.

    The per-match rows carry both clubs but not which one was the player's, so
    this is the only thing that makes home/away recoverable. A mid-season
    transfer legitimately yields two clubs for one season; callers treat that
    as unresolvable rather than guessing.
    """
    out: dict[int, set[str]] = {}
    for row in ((groups or {}).get("season") or []):
        team = row.get("team")
        if team:
            out.setdefault(_int(row.get("season")), set()).add(team)
    return out


def _fixture_date_index(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            """SELECT f.event, f.kickoff_time, th.name AS home, ta.name AS away
               FROM fixtures f
               LEFT JOIN teams th ON th.id = f.team_h
               LEFT JOIN teams ta ON ta.id = f.team_a
               WHERE f.event IS NOT NULL AND f.kickoff_time IS NOT NULL"""
        )
    ]


def _match_to_gw(conn: sqlite3.Connection, match: dict,
                 fixtures: list[dict]) -> int | None:
    """Map an Understat match to an FPL gameweek by date AND club.

    Unmatched rows keep fpl_gw = NULL rather than being guessed: a mis-assigned
    match corrupts that gameweek's variance decomposition, and a NULL is merely
    absent.
    """
    raw = match.get("date")
    if not raw:
        return None
    try:
        when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)

    home = (match.get("h_team") or "").lower()
    away = (match.get("a_team") or "").lower()

    best, best_delta = None, dt.timedelta(hours=36)
    for f in fixtures:
        try:
            kick = dt.datetime.fromisoformat(str(f["kickoff_time"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if kick.tzinfo is None:
            kick = kick.replace(tzinfo=dt.timezone.utc)

        delta = abs(kick - when)
        if delta > best_delta:
            continue
        fh = (f["home"] or "").lower()
        fa = (f["away"] or "").lower()
        if not (_club_match(home, fh) and _club_match(away, fa)):
            continue
        best, best_delta = f["event"], delta
    return best


def _club_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    return bool(a_tokens & b_tokens)


REGISTRY: dict[str, Callable] = {
    "refresh_reference": refresh_reference,
    "snapshot_prices": snapshot_prices,
    "ingest_understat_league": ingest_understat_league,
    "understat_fanout": understat_fanout,
    "resolve_entities": resolve_entities,
    "recompute_xp": recompute_xp,
    "freeze_projections": freeze_projections,
    "calibrate": calibrate,
    "discover_leagues": discover_leagues,
    "ingest_mini_league": ingest_mini_league,
    "freeze_rivals": freeze_rivals,
    "poll_live": poll_live,
}
