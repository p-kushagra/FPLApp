"""Gameweek state machine and free-transfer bank.

Fixes the v1 defect where `planner.next_gw()` inferred the planning gameweek
from `MIN(event) WHERE finished = 0`. That is wrong in three states: mid-
gameweek (it plans for the week already in progress), after a postponement (a
rearranged fixture with a low `event` drags planning backwards), and between
seasons.

Three gameweek concepts, never conflated again:

    scoring_gw       the GW FPL is scoring right now, or last scored
    anchor_gw        the GW whose deadline is next -- what you are picking for
    last_complete_gw greatest GW with finished AND data_checked

The Active Focus Rule falls out of `anchor_gw`: FPL flips `is_next` the instant
a deadline passes, so the pivot to GW+1..GW+5 needs no scheduled job. When the
cached event list is stale, `deadline_time` is compared against the wall clock
so the pivot still happens.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from enum import Enum

from . import rules as rules_mod

DEFAULT_HORIZON = 5


class Phase(str, Enum):
    PRE_SEASON = "PRE_SEASON"
    UPCOMING = "UPCOMING"    # transfers open, solver active
    LIVE = "LIVE"            # transfers closed, rival squads frozen
    SETTLING = "SETTLING"    # played out, bonus/auto-subs not final


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class GWState:
    scoring_gw: int
    anchor_gw: int
    last_complete_gw: int
    phase: Phase
    deadline: dt.datetime | None
    now: dt.datetime

    @property
    def transfers_open(self) -> bool:
        return self.phase in (Phase.UPCOMING, Phase.PRE_SEASON)

    @property
    def rivals_frozen(self) -> bool:
        """Rival squads are knowable only once the deadline has passed."""
        return self.phase in (Phase.LIVE, Phase.SETTLING)

    @property
    def seconds_to_deadline(self) -> float | None:
        if self.deadline is None:
            return None
        return (self.deadline - self.now).total_seconds()

    def planning_window(self, horizon: int = DEFAULT_HORIZON) -> list[int]:
        return list(range(self.anchor_gw, self.anchor_gw + horizon))


# --------------------------------------------------------------------------
# State derivation
# --------------------------------------------------------------------------
def sync_gw_state(conn: sqlite3.Connection, events: list[dict]) -> int:
    """Persist bootstrap-static `events` into `gw_state`. Returns rows written."""
    now = _utcnow().isoformat()
    written = 0
    for e in events:
        conn.execute(
            """INSERT INTO gw_state
                 (gw, deadline_time, is_current, is_next, finished, data_checked,
                  average_score, highest_score, most_captained, transfers_made,
                  updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(gw) DO UPDATE SET
                 deadline_time = excluded.deadline_time,
                 is_current = excluded.is_current,
                 is_next = excluded.is_next,
                 finished = excluded.finished,
                 data_checked = excluded.data_checked,
                 average_score = excluded.average_score,
                 highest_score = excluded.highest_score,
                 most_captained = excluded.most_captained,
                 transfers_made = excluded.transfers_made,
                 updated_at = excluded.updated_at""",
            (
                e["id"], e.get("deadline_time"),
                1 if e.get("is_current") else 0,
                1 if e.get("is_next") else 0,
                1 if e.get("finished") else 0,
                1 if e.get("data_checked") else 0,
                e.get("average_entry_score"), e.get("highest_score"),
                e.get("most_captained"), e.get("transfers_made"), now,
            ),
        )
        written += 1
    conn.commit()
    return written


def gw_state(conn: sqlite3.Connection, now: dt.datetime | None = None) -> GWState:
    """Derive the current temporal state from `gw_state` rows.

    Falls back to the fixtures table, then to meta.current_gw, so a database
    that predates `sync_gw_state` still yields a sane answer.
    """
    moment = now or _utcnow()
    rows = [dict(r) for r in conn.execute(
        """SELECT gw, deadline_time, is_current, is_next, finished, data_checked
           FROM gw_state ORDER BY gw"""
    )]

    if not rows:
        return _fallback_state(conn, moment)

    finished = [r["gw"] for r in rows if r["finished"]]
    complete = [r["gw"] for r in rows if r["finished"] and r["data_checked"]]

    current = next((r["gw"] for r in rows if r["is_current"]), None)
    scoring = current if current is not None else (max(finished) if finished else 0)

    # Prefer the API's own is_next, but only while its deadline is genuinely in
    # the future -- a stale bootstrap read is exactly when the pivot matters.
    anchor = None
    flagged_next = next((r for r in rows if r["is_next"]), None)
    if flagged_next is not None:
        deadline = _parse(flagged_next["deadline_time"])
        if deadline is None or deadline > moment:
            anchor = flagged_next["gw"]

    if anchor is None:
        upcoming = [r["gw"] for r in rows
                    if (_parse(r["deadline_time"]) or moment) > moment]
        anchor = min(upcoming) if upcoming else scoring + 1

    anchor_row = next((r for r in rows if r["gw"] == anchor), None)
    deadline = _parse(anchor_row["deadline_time"]) if anchor_row else None

    phase = _phase(conn, rows, scoring, anchor, moment)

    return GWState(
        scoring_gw=scoring,
        anchor_gw=anchor,
        last_complete_gw=max(complete) if complete else 0,
        phase=phase,
        deadline=deadline,
        now=moment,
    )


def _phase(conn: sqlite3.Connection, rows: list[dict], scoring: int,
           anchor: int, moment: dt.datetime) -> Phase:
    scoring_row = next((r for r in rows if r["gw"] == scoring), None)

    if scoring_row is None or scoring == 0:
        return Phase.PRE_SEASON

    # Before the anchor deadline and the scoring GW is settled -> planning.
    anchor_row = next((r for r in rows if r["gw"] == anchor), None)
    anchor_deadline = _parse(anchor_row["deadline_time"]) if anchor_row else None

    if scoring_row["finished"]:
        # Finished but bonus/auto-subs not confirmed.
        if not scoring_row["data_checked"]:
            return Phase.SETTLING
        return Phase.UPCOMING

    # Not finished. If its deadline has passed, matches are in progress.
    scoring_deadline = _parse(scoring_row["deadline_time"])
    if scoring_deadline is not None and moment >= scoring_deadline:
        return Phase.LIVE

    if anchor_deadline is not None and moment < anchor_deadline:
        return Phase.UPCOMING
    return Phase.UPCOMING


def _fallback_state(conn: sqlite3.Connection, moment: dt.datetime) -> GWState:
    """No gw_state rows: reconstruct from fixtures and meta."""
    row = conn.execute(
        "SELECT MAX(event) gw FROM fixtures WHERE finished = 1 AND event IS NOT NULL"
    ).fetchone()
    last_done = int(row["gw"]) if row and row["gw"] is not None else 0

    meta = conn.execute(
        "SELECT value FROM meta WHERE key = 'current_gw'"
    ).fetchone()
    scoring = int(meta["value"]) if meta and str(meta["value"]).isdigit() else last_done

    return GWState(
        scoring_gw=scoring,
        anchor_gw=max(scoring + 1, last_done + 1),
        last_complete_gw=last_done,
        phase=Phase.UPCOMING,
        deadline=None,
        now=moment,
    )


def scoring_gw(conn: sqlite3.Connection) -> int:
    return gw_state(conn).scoring_gw


def anchor_gw(conn: sqlite3.Connection) -> int:
    return gw_state(conn).anchor_gw


def planning_window(conn: sqlite3.Connection,
                    horizon: int = DEFAULT_HORIZON) -> list[int]:
    return gw_state(conn).planning_window(horizon)


# --------------------------------------------------------------------------
# Free-transfer bank
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FTBank:
    """State of the free-transfer bank entering a gameweek."""

    gw: int
    available: int          # f_t
    transfers_made: int = 0  # T_t
    consumed: int = 0        # q_t
    hits: int = 0            # h_t
    chip: str | None = None  # wildcard | freehit | benchboost | 3xc

    @property
    def points_cost(self) -> int:
        return self.hits * 4

    @property
    def is_squad_chip(self) -> bool:
        """Wildcard and Free Hit make transfers free; the others do not."""
        return (self.chip or "").lower().replace(" ", "") in {
            "wildcard", "freehit", "free_hit",
        }


def project_ft(bank: FTBank, transfers: int, chip: str | None = None,
               rules: dict | None = None) -> FTBank:
    """Apply one gameweek of transfer activity and return the NEXT bank.

    The recurrence, with F the cap:

        squad chip active (WC/FH):
            q = 0                       (chip_retains_ft)
            h = 0
            f' = min(F, f + accrual)    accrual = 1 if chip_accrues_ft else 0

        otherwise:
            q = min(T, f)
            h = max(0, T - f)
            f' = min(F, f - q + 1)

    With this season's verified `chip_accrues_ft: false`, a chip week freezes
    the bank exactly: f' = f. That is a different behaviour from banking a
    normal week, and it is why the flag is a rule rather than a constant.
    """
    cfg = rules_mod.transfers(rules)
    cap = int(cfg["max_banked"])
    per_gw = int(cfg["free_per_gw"])
    retains = bool(cfg["chip_retains_ft"])
    accrues = bool(cfg["chip_accrues_ft"])

    probe = FTBank(gw=bank.gw, available=bank.available, chip=chip)
    transfers = max(0, int(transfers))

    if probe.is_squad_chip:
        consumed = 0 if retains else min(transfers, bank.available)
        hits = 0
        accrual = per_gw if accrues else 0
    else:
        consumed = min(transfers, bank.available)
        hits = max(0, transfers - bank.available)
        accrual = per_gw

    next_available = min(cap, max(0, bank.available - consumed) + accrual)

    # The returned bank describes GW t+1; the activity fields record GW t.
    return FTBank(
        gw=bank.gw + 1,
        available=next_available,
        transfers_made=transfers,
        consumed=consumed,
        hits=hits,
        chip=chip,
    )


def read_ft_bank(conn: sqlite3.Connection, gw: int) -> FTBank:
    """Stored bank for `gw`, defaulting to 1 FT when unknown."""
    row = conn.execute(
        """SELECT gw, ft_available, transfers_made, ft_consumed, hits, chip_active
           FROM ft_bank WHERE gw = ?""",
        (gw,),
    ).fetchone()
    if row is None:
        return FTBank(gw=gw, available=1)
    return FTBank(
        gw=row["gw"],
        available=int(row["ft_available"]),
        transfers_made=int(row["transfers_made"] or 0),
        consumed=int(row["ft_consumed"] or 0),
        hits=int(row["hits"] or 0),
        chip=row["chip_active"],
    )


def write_ft_bank(conn: sqlite3.Connection, bank: FTBank,
                  derived: bool = False) -> None:
    conn.execute(
        """INSERT INTO ft_bank
             (gw, ft_available, transfers_made, ft_consumed, hits, chip_active,
              derived, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(gw) DO UPDATE SET
             ft_available = excluded.ft_available,
             transfers_made = excluded.transfers_made,
             ft_consumed = excluded.ft_consumed,
             hits = excluded.hits,
             chip_active = excluded.chip_active,
             derived = excluded.derived,
             updated_at = excluded.updated_at""",
        (bank.gw, bank.available, bank.transfers_made, bank.consumed,
         bank.hits, bank.chip, 1 if derived else 0, _utcnow().isoformat()),
    )
    conn.commit()


def rebuild_ft_ledger(conn: sqlite3.Connection, history: list[dict],
                      start_ft: int = 1, rules: dict | None = None) -> list[FTBank]:
    """Replay `entry/{id}/history/` into a full FT ledger.

    FPL reports `event_transfers` and `event_transfers_cost` per gameweek but
    never the bank itself, so the bank is derived by replaying the recurrence
    from GW1. Where the derived hit disagrees with the reported cost, the
    REPORTED figure wins and the bank is re-anchored -- FPL is authoritative
    about what it charged, and a silent divergence would compound all season.
    """
    ledger: list[FTBank] = []
    bank = FTBank(gw=int(history[0]["event"]) if history else 1, available=start_ft)

    for row in history:
        gw = int(row["event"])
        made = int(row.get("event_transfers") or 0)
        reported_cost = int(row.get("event_transfers_cost") or 0)
        chip = row.get("chip") or None

        entering = FTBank(gw=gw, available=bank.available)
        nxt = project_ft(entering, made, chip, rules)

        if nxt.hits * 4 != reported_cost:
            # Trust FPL's charge; back out the implied bank for this gameweek.
            actual_hits = reported_cost // 4
            implied_available = max(0, made - actual_hits)
            entering = FTBank(gw=gw, available=implied_available)
            nxt = project_ft(entering, made, chip, rules)

        ledger.append(FTBank(gw=gw, available=entering.available,
                             transfers_made=made, consumed=nxt.consumed,
                             hits=nxt.hits, chip=chip))
        bank = nxt

    ledger.append(bank)
    return ledger
