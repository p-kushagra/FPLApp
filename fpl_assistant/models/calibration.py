"""Calibration gate: does the structured xP model actually beat the baseline?

A projection nobody has scored is an opinion. This module scores it, on two
gates that a model can only pass by being genuinely informative:

* **Decile monotonicity.** Sort players by projected xP, cut into ten buckets,
  and require realised points to rise across them. This tests *ranking*, which
  is what transfer and captaincy decisions actually consume -- a model can have
  a fine RMSE and still order players wrongly.
* **Error benchmark.** RMSE against a baseline. The reference baseline is FPL's
  own `ep_next`, which is the honest comparator: it is free, it ships with the
  API, and a bespoke model that cannot beat it is not earning its complexity.

Everything here is strictly out-of-sample. A projection for gameweek *g* is
rebuilt with `as_of=g-1`, so it sees only gameweeks that had finished when the
forecast would have been made. Live injury fields are neutralised for the same
reason -- they are a *current* snapshot with no history behind them, so
replaying a past gameweek would otherwise stamp today's injuries onto it.

The one leak this cannot close by recomputation is `ep_next`: FPL overwrites it
every gameweek and keeps no history, so it is only comparable for gameweeks the
snapshot pipeline froze. For earlier gameweeks the gate falls back to computable
baselines and says so, rather than quietly comparing against nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import statistics
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from ..rules import ELEMENT_TYPE_TO_POS
from . import snapshot as snapshot_mod
from . import xp as xp_mod

# Ten deciles is the FPL convention and keeps ~60 players per bucket on a full
# player set -- enough that a bucket mean is not one hauler.
N_DECILES = 10

# Strict monotonicity across ten noisy buckets is a coin-flip even for a good
# model, so the gate allows a small number of local inversions and additionally
# requires the rank correlation of the bucket means to be strongly positive.
MAX_DECILE_INVERSIONS = 1
MIN_DECILE_SPEARMAN = 0.9

# Below this the gate reports "insufficient evidence" rather than a verdict. One
# gameweek of history cannot distinguish a good model from a lucky one.
MIN_GWS_FOR_VERDICT = 3
MIN_ROWS_FOR_VERDICT = 500

# An affine recalibration fitted on fewer folds than this is withheld: with one
# fold the slope is fitting that gameweek's variance, not the model's bias.
MIN_GWS_FOR_FIT = 4

BASELINE_EP_NEXT = "ep_next"
BASELINE_PPG = "ppg_to_date"
BASELINE_LAST_GW = "last_gw_points"
BASELINE_POSITIONAL = "positional_mean"


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------
@dataclass
class Observation:
    """One player-gameweek: what we forecast, and what happened."""
    player_id: int
    gw: int
    position: str
    xp: float
    actual: float
    minutes: int
    source: str
    ep_next: float | None = None
    ppg_to_date: float | None = None
    last_gw_points: float | None = None
    from_snapshot: bool = False


def _positions(conn: sqlite3.Connection) -> dict[int, str]:
    out: dict[int, str] = {}
    for r in conn.execute("SELECT id, element_type, position FROM players"):
        etype = r["element_type"]
        pos = ELEMENT_TYPE_TO_POS.get(etype) if etype is not None else None
        out[int(r["id"])] = pos or (r["position"] or "MID")
    return out


def _actuals(conn: sqlite3.Connection, gw: int) -> dict[int, sqlite3.Row]:
    return {int(r["player_id"]): r for r in conn.execute(
        "SELECT player_id, total_points, minutes FROM player_gw WHERE gw = ?",
        (gw,))}


def _prior_form(conn: sqlite3.Connection, gw: int
                ) -> tuple[dict[int, float], dict[int, float]]:
    """Points-per-appearance and previous-gameweek points, both as of gw-1."""
    ppg: dict[int, float] = {}
    for r in conn.execute(
            """SELECT player_id, SUM(total_points) pts, COUNT(*) n
               FROM player_gw WHERE gw < ? GROUP BY player_id""", (gw,)):
        n = int(r["n"] or 0)
        if n:
            ppg[int(r["player_id"])] = float(r["pts"] or 0) / n

    last = {int(r["player_id"]): float(r["total_points"] or 0)
            for r in conn.execute(
                "SELECT player_id, total_points FROM player_gw WHERE gw = ?",
                (gw - 1,))}
    return ppg, last


def observations(conn: sqlite3.Connection, gws: Sequence[int],
                 *, played_only: bool = True,
                 prefer_snapshot: bool = True) -> list[Observation]:
    """Build the evaluation set for `gws`, strictly out-of-sample.

    A frozen snapshot is used when one exists -- it is the real forecast, made
    before kickoff, and it carries the matching `ep_next`. Otherwise the
    projection is rebuilt with the gameweek's own results withheld, which is a
    faithful replay of the model but cannot recover the baseline.
    """
    positions = _positions(conn)
    out: list[Observation] = []

    for gw in gws:
        actual = _actuals(conn, gw)
        if not actual:
            continue
        ppg, last = _prior_form(conn, gw)

        frozen = (snapshot_mod.load(conn, gw)
                  if prefer_snapshot and snapshot_mod.has_snapshot(conn, gw)
                  else [])

        if frozen:
            rows = [(int(r["player_id"]), float(r["xp_total"] or 0.0),
                     str(r["source"] or ""), _num(r["ep_next"])) for r in frozen]
            from_snapshot = True
        else:
            # Rebuild with the target gameweek withheld. This is the whole
            # anti-leakage contract of the module in one call.
            projected = xp_mod.project(
                conn, [gw], persist=False,
                as_of=gw - 1, neutralise_availability=True)
            rows = [(pid, float(b.total), b.source, None)
                    for (pid, bgw), b in projected.items() if bgw == gw]
            from_snapshot = False

        for pid, xp_total, source, ep in rows:
            hit = actual.get(pid)
            if hit is None:
                continue
            minutes = int(hit["minutes"] or 0)
            if played_only and minutes <= 0:
                # Non-participants are not a forecasting failure -- including
                # 400 guaranteed zeroes would flatter every model equally and
                # drown the signal we are trying to measure.
                continue
            out.append(Observation(
                player_id=pid, gw=gw, position=positions.get(pid, "MID"),
                xp=xp_total, actual=float(hit["total_points"] or 0),
                minutes=minutes, source=source, ep_next=ep,
                ppg_to_date=ppg.get(pid), last_gw_points=last.get(pid),
                from_snapshot=from_snapshot))

    return out


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def rmse(pred: Sequence[float], obs: Sequence[float]) -> float:
    if not pred:
        return float("nan")
    return math.sqrt(sum((p - a) ** 2 for p, a in zip(pred, obs)) / len(pred))


def mae(pred: Sequence[float], obs: Sequence[float]) -> float:
    if not pred:
        return float("nan")
    return sum(abs(p - a) for p, a in zip(pred, obs)) / len(pred)


def bias(pred: Sequence[float], obs: Sequence[float]) -> float:
    """Mean signed error. Positive means the model over-forecasts."""
    if not pred:
        return float("nan")
    return sum(p - a for p, a in zip(pred, obs)) / len(pred)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) < 2:
        return float("nan")
    ra, rb = _ranks(a), _ranks(b)
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return float("nan")


@dataclass
class Decile:
    index: int
    n: int
    mean_xp: float
    mean_actual: float
    min_xp: float
    max_xp: float


@dataclass
class DecileReport:
    deciles: list[Decile] = field(default_factory=list)
    inversions: list[int] = field(default_factory=list)
    spearman: float = float("nan")
    monotonic: bool = False
    lift: float = float("nan")   # top decile mean minus bottom decile mean


def deciles(obs: Sequence[Observation], n: int = N_DECILES) -> DecileReport:
    """Bucket by projected xP and report realised points per bucket."""
    report = DecileReport()
    rows = sorted(obs, key=lambda o: o.xp)
    if len(rows) < n:
        return report

    size = len(rows) / n
    buckets: list[list[Observation]] = []
    for i in range(n):
        lo, hi = round(i * size), round((i + 1) * size)
        buckets.append(rows[lo:hi])

    for i, bucket in enumerate(buckets, start=1):
        if not bucket:
            continue
        report.deciles.append(Decile(
            index=i, n=len(bucket),
            mean_xp=round(sum(o.xp for o in bucket) / len(bucket), 3),
            mean_actual=round(sum(o.actual for o in bucket) / len(bucket), 3),
            min_xp=round(min(o.xp for o in bucket), 3),
            max_xp=round(max(o.xp for o in bucket), 3),
        ))

    means = [d.mean_actual for d in report.deciles]
    report.inversions = [d.index for prev, d in zip(report.deciles,
                                                    report.deciles[1:])
                         if d.mean_actual < prev.mean_actual]
    report.spearman = round(
        spearman([float(d.index) for d in report.deciles], means), 4)
    if means:
        report.lift = round(means[-1] - means[0], 3)
    report.monotonic = (
        len(report.inversions) <= MAX_DECILE_INVERSIONS
        and not math.isnan(report.spearman)
        and report.spearman >= MIN_DECILE_SPEARMAN
    )
    return report


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------
def _baseline_series(obs: Sequence[Observation], name: str
                     ) -> tuple[list[float], list[float], str | None]:
    """(predictions, matching actuals, unavailability reason)."""
    pred: list[float] = []
    act: list[float] = []

    if name == BASELINE_POSITIONAL:
        by_pos: dict[str, list[float]] = {}
        for o in obs:
            by_pos.setdefault(o.position, []).append(o.actual)
        means = {p: sum(v) / len(v) for p, v in by_pos.items() if v}
        for o in obs:
            pred.append(means.get(o.position, 0.0))
            act.append(o.actual)
        return pred, act, None

    attr = {BASELINE_EP_NEXT: "ep_next",
            BASELINE_PPG: "ppg_to_date",
            BASELINE_LAST_GW: "last_gw_points"}[name]
    for o in obs:
        value = getattr(o, attr)
        if value is None:
            continue
        pred.append(float(value))
        act.append(o.actual)

    if not pred:
        if name == BASELINE_EP_NEXT:
            return [], [], (
                "FPL rewrites ep_next every gameweek and keeps no history, so "
                "it is only comparable for gameweeks the snapshot pipeline "
                "froze. No frozen gameweek in this window.")
        return [], [], f"no rows carry {attr}"
    return pred, act, None


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
@dataclass
class BaselineResult:
    name: str
    n: int = 0
    rmse: float = float("nan")
    mae: float = float("nan")
    beaten: bool = False
    unavailable: str | None = None


@dataclass
class CalibrationReport:
    run_id: str
    created_at: str
    gws: list[int]
    n_rows: int = 0
    n_snapshot_gws: int = 0
    rmse: float = float("nan")
    mae: float = float("nan")
    bias: float = float("nan")
    spearman: float = float("nan")
    decile: DecileReport = field(default_factory=DecileReport)
    baselines: list[BaselineResult] = field(default_factory=list)
    source_mix: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    verdict: str = "INSUFFICIENT_EVIDENCE"

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    @property
    def reference(self) -> BaselineResult | None:
        """The gate's own comparator: ep_next when frozen, else best available."""
        for b in self.baselines:
            if b.name == BASELINE_EP_NEXT and b.unavailable is None:
                return b
        usable = [b for b in self.baselines if b.unavailable is None]
        if not usable:
            return None
        return min(usable, key=lambda b: b.rmse)


def evaluate(conn: sqlite3.Connection, gws: Sequence[int] | None = None,
             *, played_only: bool = True,
             prefer_snapshot: bool = True) -> CalibrationReport:
    """Run both gates over `gws` (default: every gameweek that can be scored)."""
    if gws is None:
        gws = evaluable_gws(conn)
    gws = list(gws)

    report = CalibrationReport(
        run_id=uuid.uuid4().hex[:12],
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        gws=gws)

    obs = observations(conn, gws, played_only=played_only,
                       prefer_snapshot=prefer_snapshot)
    report.n_rows = len(obs)
    if not obs:
        report.blockers.append("no scoreable player-gameweeks in this window")
        return report

    report.n_snapshot_gws = len({o.gw for o in obs if o.from_snapshot})
    for o in obs:
        report.source_mix[o.source] = report.source_mix.get(o.source, 0) + 1

    pred = [o.xp for o in obs]
    act = [o.actual for o in obs]
    report.rmse = round(rmse(pred, act), 4)
    report.mae = round(mae(pred, act), 4)
    report.bias = round(bias(pred, act), 4)
    report.spearman = round(spearman(pred, act), 4)
    report.decile = deciles(obs)

    for name in (BASELINE_EP_NEXT, BASELINE_PPG, BASELINE_LAST_GW,
                 BASELINE_POSITIONAL):
        bp, ba, missing = _baseline_series(obs, name)
        if missing is not None:
            report.baselines.append(BaselineResult(name, unavailable=missing))
            continue
        # Score the model on exactly the rows the baseline covers, or the
        # comparison is between two different populations.
        covered = [o for o in obs if _has(o, name)]
        model_rmse = rmse([o.xp for o in covered], [o.actual for o in covered])
        b_rmse = rmse(bp, ba)
        report.baselines.append(BaselineResult(
            name=name, n=len(bp), rmse=round(b_rmse, 4), mae=round(mae(bp, ba), 4),
            beaten=bool(model_rmse < b_rmse)))

    _apply_gates(report)
    return report


def _has(o: Observation, baseline: str) -> bool:
    if baseline == BASELINE_POSITIONAL:
        return True
    return getattr(o, {BASELINE_EP_NEXT: "ep_next",
                       BASELINE_PPG: "ppg_to_date",
                       BASELINE_LAST_GW: "last_gw_points"}[baseline]) is not None


def _apply_gates(report: CalibrationReport) -> None:
    """Set the verdict. Refuses to pass or fail on evidence it does not have."""
    if not report.decile.monotonic:
        report.blockers.append(
            f"decile monotonicity: {len(report.decile.inversions)} inversion(s) "
            f"at decile(s) {report.decile.inversions or '-'}, "
            f"rank correlation {report.decile.spearman} "
            f"(need <= {MAX_DECILE_INVERSIONS} and >= {MIN_DECILE_SPEARMAN})")

    ref = report.reference
    if ref is None:
        report.blockers.append("no usable baseline to benchmark against")
    elif not ref.beaten:
        report.blockers.append(
            f"error benchmark: model RMSE {report.rmse} does not beat "
            f"{ref.name} RMSE {ref.rmse}")

    ep = next((b for b in report.baselines if b.name == BASELINE_EP_NEXT), None)
    if ep is not None and ep.unavailable is not None:
        report.blockers.append(f"reference baseline ep_next unavailable: "
                               f"{ep.unavailable}")

    n_gws = len({g for g in report.gws})
    thin = (report.n_rows < MIN_ROWS_FOR_VERDICT
            or n_gws < MIN_GWS_FOR_VERDICT)

    if thin:
        report.verdict = "INSUFFICIENT_EVIDENCE"
        report.blockers.append(
            f"sample too thin for a verdict: {report.n_rows} rows over "
            f"{n_gws} gameweek(s); need >= {MIN_ROWS_FOR_VERDICT} rows over "
            f">= {MIN_GWS_FOR_VERDICT} gameweeks")
        return

    report.verdict = "PASS" if not report.blockers else "FAIL"


def evaluable_gws(conn: sqlite3.Connection) -> list[int]:
    """Gameweeks with results, excluding GW1 which has no prior history."""
    return [int(r[0]) for r in conn.execute(
        """SELECT DISTINCT gw FROM player_gw
           WHERE gw > 1 AND gw <= (SELECT MAX(gw) FROM player_gw)
           ORDER BY gw""")]


# --------------------------------------------------------------------------
# Affine recalibration
# --------------------------------------------------------------------------
@dataclass
class Fit:
    position: str
    intercept: float
    slope: float
    n_rows: int
    n_gws: int
    rmse_before: float
    rmse_after: float
    applied: bool


def fit_affine(conn: sqlite3.Connection, report: CalibrationReport,
               obs: Sequence[Observation] | None = None,
               *, persist: bool = True) -> list[Fit]:
    """Least-squares `actual ~ a + b*xp` per position.

    Deliberately the smallest correction that can help: two parameters per
    position. At the sample sizes this project will have for most of a season,
    anything richer fits noise. The fit is stored but only marked `applied` once
    enough independent gameweeks back it, so a promising one-fold result cannot
    quietly start moving recommendations.
    """
    obs = list(obs if obs is not None else observations(conn, report.gws))
    n_gws = len({o.gw for o in obs})
    applied = n_gws >= MIN_GWS_FOR_FIT

    by_pos: dict[str, list[Observation]] = {}
    for o in obs:
        by_pos.setdefault(o.position, []).append(o)

    fits: list[Fit] = []
    for pos, rows in sorted(by_pos.items()):
        xs = [o.xp for o in rows]
        ys = [o.actual for o in rows]
        n = len(rows)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        slope = (sxy / sxx) if sxx > 1e-9 else 1.0
        intercept = mean_y - slope * mean_x

        before = rmse(xs, ys)
        after = rmse([intercept + slope * x for x in xs], ys)
        fits.append(Fit(pos, round(intercept, 4), round(slope, 4), n, n_gws,
                        round(before, 4), round(after, 4), applied))

    if persist:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with conn:
            conn.executemany(
                """INSERT OR REPLACE INTO calibration_fit
                     (position, intercept, slope, n_rows, n_gws, rmse_before,
                      rmse_after, applied, run_id, fitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(f.position, f.intercept, f.slope, f.n_rows, f.n_gws,
                  f.rmse_before, f.rmse_after, int(f.applied),
                  report.run_id, now) for f in fits])
    return fits


def active_fit(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """Positions whose recalibration has cleared the sample-size gate."""
    return {r["position"]: (float(r["intercept"]), float(r["slope"]))
            for r in conn.execute(
                "SELECT * FROM calibration_fit WHERE applied = 1")}


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def persist(conn: sqlite3.Connection, report: CalibrationReport) -> None:
    ref = report.reference
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO calibration_run
                 (run_id, created_at, gws, n_rows, rmse_model, mae_model,
                  bias_model, spearman_model, baseline_name, rmse_baseline,
                  mae_baseline, decile_monotonic, decile_spearman, passed,
                  blockers, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report.run_id, report.created_at, json.dumps(report.gws),
             report.n_rows, report.rmse, report.mae, report.bias,
             report.spearman,
             ref.name if ref else None, ref.rmse if ref else None,
             ref.mae if ref else None,
             int(report.decile.monotonic), report.decile.spearman,
             int(report.passed), json.dumps(report.blockers),
             json.dumps(asdict(report), default=str)))


def latest_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM calibration_run ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def format_report(report: CalibrationReport, fits: Sequence[Fit] = ()) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 74)
    add("  CALIBRATION GATE")
    add("=" * 74)
    add(f"  run          {report.run_id}   {report.created_at[:19]}Z")
    add(f"  gameweeks    {report.gws or '-'}"
        f"   ({report.n_snapshot_gws} from frozen snapshots)")
    add(f"  rows scored  {report.n_rows}")
    if report.source_mix:
        add("  rate source  " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.source_mix.items())))
    add("")

    add("  MODEL ERROR")
    add(f"    RMSE {report.rmse:>8}    MAE {report.mae:>8}"
        f"    bias {report.bias:>+8}    rank-corr {report.spearman:>7}")
    add("")

    add("  GATE 2 - ERROR BENCHMARK (RMSE_model vs baselines)")
    add(f"    {'baseline':<18}{'n':>7}{'RMSE':>10}{'MAE':>10}   verdict")
    for b in report.baselines:
        if b.unavailable:
            add(f"    {b.name:<18}{'-':>7}{'-':>10}{'-':>10}   unavailable")
            continue
        mark = "model wins" if b.beaten else "BASELINE WINS"
        add(f"    {b.name:<18}{b.n:>7}{b.rmse:>10}{b.mae:>10}   {mark}")
    for b in report.baselines:
        if b.unavailable:
            add(f"      ! {b.name}: {b.unavailable}")
    add("")

    add("  GATE 1 - DECILE MONOTONICITY (bucketed by projected xP)")
    if not report.decile.deciles:
        add("    not enough rows to form deciles")
    else:
        add(f"    {'decile':>7}{'n':>7}{'xP range':>18}{'mean xP':>10}"
            f"{'mean actual':>14}")
        prev = None
        for d in report.decile.deciles:
            arrow = " " if prev is None else (
                "up" if d.mean_actual >= prev else "DOWN")
            add(f"    {d.index:>7}{d.n:>7}"
                f"{f'{d.min_xp:.2f}-{d.max_xp:.2f}':>18}"
                f"{d.mean_xp:>10.2f}{d.mean_actual:>14.2f}   {arrow}")
            prev = d.mean_actual
        add(f"    inversions {report.decile.inversions or 'none'}"
            f"   rank-corr {report.decile.spearman}"
            f"   top-vs-bottom lift {report.decile.lift:+.2f} pts")
        add(f"    monotonic: {'YES' if report.decile.monotonic else 'NO'}")
    add("")

    if fits:
        add("  AFFINE RECALIBRATION (actual ~ a + b*xP, per position)")
        add(f"    {'pos':<6}{'intercept':>12}{'slope':>10}{'n':>8}"
            f"{'RMSE before':>14}{'RMSE after':>13}   status")
        for f in fits:
            status = "APPLIED" if f.applied else "withheld (too few GWs)"
            add(f"    {f.position:<6}{f.intercept:>12.3f}{f.slope:>10.3f}"
                f"{f.n_rows:>8}{f.rmse_before:>14.3f}{f.rmse_after:>13.3f}"
                f"   {status}")
        add("")

    add("-" * 74)
    add(f"  VERDICT: {report.verdict}")
    for blocker in report.blockers:
        add(f"    - {blocker}")
    add("=" * 74)
    return "\n".join(lines)


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    from ..config import load_config
    from ..db import connect

    parser = argparse.ArgumentParser(
        prog="python -m fpl_assistant.models.calibration",
        description="Score the xP model against realised points.")
    parser.add_argument("--gw", type=int, action="append", dest="gws",
                        help="gameweek to evaluate (repeatable)")
    parser.add_argument("--all-players", action="store_true",
                        help="include players who did not appear")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="ignore frozen snapshots and replay the model")
    parser.add_argument("--fit", action="store_true",
                        help="also fit the affine recalibration")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()
    conn = connect(cfg.db_path)
    report = evaluate(conn, args.gws, played_only=not args.all_players,
                      prefer_snapshot=not args.no_snapshot)
    fits = fit_affine(conn, report) if args.fit else []
    persist(conn, report)

    if args.json:
        print(json.dumps({"report": asdict(report),
                          "fits": [asdict(f) for f in fits]}, default=str,
                         indent=2))
    else:
        print(format_report(report, fits))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
