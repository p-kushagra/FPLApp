"""Transfer-market momentum and nightly price-change forecasting.

FPL moves a price when net transfers cross a threshold set by how many managers
already own the player. That is why raw net transfers are a poor signal on their
own: 50,000 net transfers in is a stampede for a 2%-owned differential and noise
for a 60%-owned template pick. The quantity that actually predicts a change is
therefore normalised by the ownership base:

    v_net = (transfers_in - transfers_out) / ownership

which this module reports as `velocity`, expressed in net transfers per point of
ownership percentage.

Two refinements sit on top of that, and both matter:

* **Flow rate, not stock.** `transfers_in_event` is a running total for the
  gameweek, so its raw value says more about how long the gameweek has been open
  than about current momentum. Where two or more `price_snapshot` rows exist,
  the model differentiates them into a per-hour rate. A player who banked all
  their transfers on Saturday and has since gone quiet is not rising tonight,
  and only the derivative can tell you that.
* **Distance to the threshold.** The forecast is expressed as progress toward
  the change threshold, so "80% of the way there with six hours to go" is
  distinguishable from "just crossed it".

FPL does not publish the threshold and rescales it during the season, so the
constant here is a community-calibrated estimate, refined against this database's
own observed `price_change` rows once enough have accumulated. Every prediction
carries its `basis`, so a forecast made from a single snapshot is never mistaken
for one made from a real time series.
"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3
from dataclasses import dataclass, field

# Net transfers per point of ownership percentage needed to move a price. FPL
# keeps the real number secret; this is the community consensus starting point
# and is re-fitted by `calibrate_threshold` once observed changes exist.
DEFAULT_THRESHOLD = 8000.0

# Price changes are applied in a nightly batch at ~01:30 UTC.
CHANGE_HOUR_UTC = 1
CHANGE_MINUTE_UTC = 30

# A player who just changed price is locked for a day, so momentum accumulated
# before the change must not be counted toward the next one.
LOCKOUT_HOURS = 22.0

# Below this ownership the denominator is unstable -- a 0.1%-owned player needs
# almost no transfers to look explosive -- so the velocity is damped.
MIN_OWNERSHIP = 0.3

BASIS_TIME_SERIES = "flow_rate"
BASIS_EVENT_TOTAL = "event_total"
BASIS_NONE = "no_data"

DIRECTION_RISE = "rise"
DIRECTION_FALL = "fall"
DIRECTION_HOLD = "hold"


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


@dataclass
class PriceForecast:
    """One player's momentum and tonight's price call."""

    player_id: int
    player: str = ""
    team: str = ""
    position: str = ""
    now_cost: float = 0.0
    ownership: float = 0.0

    transfers_in: int = 0
    transfers_out: int = 0
    net_transfers: int = 0
    velocity: float = 0.0        # net transfers per ownership point
    flow_per_hour: float = 0.0   # net transfers per hour, when derivable

    progress: float = 0.0        # signed fraction of the threshold, -1..+1 and beyond
    p_rise: float = 0.0
    p_fall: float = 0.0
    direction: str = DIRECTION_HOLD
    hours_to_change: float | None = None
    hours_since_change: float | None = None
    locked: bool = False
    basis: str = BASIS_NONE
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        strength = max(self.p_rise, self.p_fall)
        if self.basis == BASIS_NONE or strength < 0.35:
            return "low"
        if self.basis == BASIS_EVENT_TOTAL or strength < 0.7:
            return "medium"
        return "high"

    @property
    def action(self) -> str:
        """What the forecast implies for team value, which is the whole point."""
        if self.direction == DIRECTION_RISE:
            return "Buy now to bank the rise"
        if self.direction == DIRECTION_FALL:
            return "Sell now to avoid the drop"
        return "Hold"


# --------------------------------------------------------------------------
# Core arithmetic
# --------------------------------------------------------------------------
def velocity(transfers_in: int, transfers_out: int, ownership: float) -> float:
    """v_net = (transfers_in - transfers_out) / ownership.

    Ownership is floored at `MIN_OWNERSHIP` so a near-zero denominator cannot
    manufacture an enormous velocity out of a handful of transfers.
    """
    net = float(transfers_in or 0) - float(transfers_out or 0)
    return net / max(float(ownership or 0.0), MIN_OWNERSHIP)


def _logistic(x: float, steepness: float = 6.0) -> float:
    """Map threshold progress onto a probability, saturating either side."""
    return 1.0 / (1.0 + math.exp(-steepness * (x - 1.0)))


def hours_to_next_change(now: dt.datetime | None = None) -> float:
    """Hours until the next nightly price-change batch."""
    now = now or _utcnow()
    target = now.replace(hour=CHANGE_HOUR_UTC, minute=CHANGE_MINUTE_UTC,
                         second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# Flow rate from the snapshot time series
# --------------------------------------------------------------------------
def _flow_rates(conn: sqlite3.Connection) -> dict[int, tuple[float, float]]:
    """`{player_id: (net_per_hour, hours_spanned)}` from consecutive snapshots.

    Returns an empty mapping when fewer than two distinct capture times exist,
    which is the honest answer on a fresh database and makes the caller fall
    back to event totals rather than inventing a derivative.
    """
    times = [r[0] for r in conn.execute(
        "SELECT DISTINCT captured_at FROM price_snapshot "
        "ORDER BY captured_at DESC LIMIT 2")]
    if len(times) < 2:
        return {}

    newest, previous = _parse(times[0]), _parse(times[1])
    if newest is None or previous is None:
        return {}
    hours = (newest - previous).total_seconds() / 3600.0
    if hours <= 0:
        return {}

    older = {int(r["player_id"]): float(r["net_transfers"] or 0)
             for r in conn.execute(
                 "SELECT player_id, net_transfers FROM price_snapshot "
                 "WHERE captured_at = ?", (times[1],))}

    out: dict[int, tuple[float, float]] = {}
    for r in conn.execute(
            "SELECT player_id, net_transfers FROM price_snapshot "
            "WHERE captured_at = ?", (times[0],)):
        pid = int(r["player_id"])
        current = float(r["net_transfers"] or 0)
        before = older.get(pid)
        if before is None:
            continue
        # A gameweek rollover resets the event counters, so a large negative
        # delta is a reset rather than a mass exodus. Treat it as no evidence.
        delta = current - before
        if delta < 0 and abs(delta) > abs(before) * 0.9 and before > 0:
            continue
        out[pid] = (delta / hours, hours)
    return out


def _last_change_hours(conn: sqlite3.Connection,
                       now: dt.datetime) -> dict[int, float]:
    """Hours since each player's most recent price change."""
    out: dict[int, float] = {}
    for r in conn.execute(
            """SELECT player_id, MAX(changed_at) changed_at
               FROM price_change GROUP BY player_id"""):
        when = _parse(r["changed_at"])
        if when is not None:
            out[int(r["player_id"])] = (now - when).total_seconds() / 3600.0
    return out


def calibrate_threshold(conn: sqlite3.Connection) -> float:
    """Re-fit the change threshold from this database's observed changes.

    Uses the median velocity recorded at the moment of a real price change,
    which is the only ground truth available locally. Falls back to the
    community default until enough changes have been observed for the median
    to mean anything.
    """
    values = [float(r[0]) for r in conn.execute(
        """SELECT ABS(momentum_at_change) FROM price_change
           WHERE momentum_at_change IS NOT NULL AND momentum_at_change != 0""")]
    if len(values) < 10:
        return DEFAULT_THRESHOLD
    values.sort()
    mid = len(values) // 2
    median = (values[mid] if len(values) % 2
              else (values[mid - 1] + values[mid]) / 2.0)
    return median if median > 0 else DEFAULT_THRESHOLD


# --------------------------------------------------------------------------
# Forecasting
# --------------------------------------------------------------------------
def forecast(conn: sqlite3.Connection, *, now: dt.datetime | None = None,
             threshold: float | None = None,
             persist: bool = True) -> list[PriceForecast]:
    """Momentum and tonight's price call for every player."""
    now = now or _utcnow()
    threshold = threshold if threshold is not None else calibrate_threshold(conn)
    flows = _flow_rates(conn)
    since_change = _last_change_hours(conn, now)
    to_change = hours_to_next_change(now)

    out: list[PriceForecast] = []
    for r in conn.execute(
            """SELECT p.id, p.web_name, p.position, p.now_cost,
                      p.selected_by_percent, p.transfers_in_event,
                      p.transfers_out_event, t.short_name AS team
               FROM players p LEFT JOIN teams t ON t.id = p.team_id"""):
        pid = int(r["id"])
        tin = int(r["transfers_in_event"] or 0)
        tout = int(r["transfers_out_event"] or 0)
        own = float(r["selected_by_percent"] or 0.0)

        f = PriceForecast(
            player_id=pid, player=r["web_name"] or "", team=r["team"] or "",
            position=r["position"] or "", now_cost=float(r["now_cost"] or 0.0),
            ownership=own, transfers_in=tin, transfers_out=tout,
            net_transfers=tin - tout,
            velocity=round(velocity(tin, tout, own), 1),
            hours_to_change=round(to_change, 1),
            hours_since_change=since_change.get(pid),
        )

        flow = flows.get(pid)
        if flow is not None:
            per_hour, _spanned = flow
            f.flow_per_hour = round(per_hour, 1)
            f.basis = BASIS_TIME_SERIES
            # Project the flow forward to the batch, then normalise it the same
            # way the raw velocity is normalised.
            projected = per_hour * to_change
            f.progress = (velocity(0, 0, own)
                          + (projected / max(own, MIN_OWNERSHIP))) / threshold
        elif tin or tout:
            f.basis = BASIS_EVENT_TOTAL
            f.progress = f.velocity / threshold
            f.notes.append("single snapshot - momentum is a gameweek total, "
                           "not a current rate")
        else:
            f.basis = BASIS_NONE

        f.locked = (f.hours_since_change is not None
                    and f.hours_since_change < LOCKOUT_HOURS)
        if f.locked:
            f.notes.append(
                f"changed {f.hours_since_change:.0f}h ago - locked until the "
                "next cycle")

        _apply_direction(f)
        out.append(f)

    if persist:
        _persist(conn, out, now)
    return out


def _apply_direction(f: PriceForecast) -> None:
    """Turn signed threshold progress into rise/fall probabilities."""
    if f.basis == BASIS_NONE or f.locked:
        f.p_rise = f.p_fall = 0.0
        f.direction = DIRECTION_HOLD
        return

    magnitude = abs(f.progress)
    probability = _logistic(magnitude)
    if f.progress > 0:
        f.p_rise, f.p_fall = round(probability, 3), 0.0
        f.direction = DIRECTION_RISE if probability >= 0.5 else DIRECTION_HOLD
    elif f.progress < 0:
        f.p_rise, f.p_fall = 0.0, round(probability, 3)
        f.direction = DIRECTION_FALL if probability >= 0.5 else DIRECTION_HOLD
    else:
        f.direction = DIRECTION_HOLD
    f.progress = round(f.progress, 3)


def _persist(conn: sqlite3.Connection, forecasts: list[PriceForecast],
             now: dt.datetime) -> None:
    stamp = now.isoformat()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO price_prediction
                 (player_id, momentum, momentum_rate, p_rise, p_fall,
                  hours_since_change, model, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(f.player_id, f.velocity, f.flow_per_hour, f.p_rise, f.p_fall,
              f.hours_since_change, f.basis, stamp) for f in forecasts])


# --------------------------------------------------------------------------
# Ticker views
# --------------------------------------------------------------------------
@dataclass
class Ticker:
    """What the Command Center's transfer-market strip renders."""

    rising: list[PriceForecast] = field(default_factory=list)
    falling: list[PriceForecast] = field(default_factory=list)
    migration: list[dict] = field(default_factory=list)
    hours_to_change: float = 0.0
    basis: str = BASIS_NONE
    threshold: float = DEFAULT_THRESHOLD
    owned_rising: list[PriceForecast] = field(default_factory=list)
    owned_falling: list[PriceForecast] = field(default_factory=list)

    @property
    def caveat(self) -> str | None:
        if self.basis == BASIS_TIME_SERIES:
            return None
        if self.basis == BASIS_EVENT_TOTAL:
            return ("Only one price snapshot exists, so momentum is this "
                    "gameweek's running total rather than a current flow rate. "
                    "Run the price snapshot job a few hours apart and these "
                    "become real per-hour rates.")
        return "No transfer-flow data yet - run the price snapshot job."


PRICE_BRACKETS = (
    ("Budget (<= 5.0m)", 0.0, 5.0),
    ("Mid (5.1-8.0m)", 5.0, 8.0),
    ("Premium (8.1-11.0m)", 8.0, 11.0),
    ("Elite (> 11.0m)", 11.0, 99.0),
)


def ownership_migration(forecasts: list[PriceForecast]) -> list[dict]:
    """Net transfer flow aggregated by price bracket.

    Shows where the market's money is moving -- a mass exodus from premiums
    into mid-price assets is the signature of an approaching template shift,
    and it is invisible when you only look at individual players.
    """
    out = []
    for label, low, high in PRICE_BRACKETS:
        members = [f for f in forecasts if low < f.now_cost <= high]
        if not members:
            continue
        net = sum(f.net_transfers for f in members)
        out.append({
            "bracket": label,
            "players": len(members),
            "net_transfers": net,
            "transfers_in": sum(f.transfers_in for f in members),
            "transfers_out": sum(f.transfers_out for f in members),
            "rising": sum(1 for f in members if f.direction == DIRECTION_RISE),
            "falling": sum(1 for f in members if f.direction == DIRECTION_FALL),
            "flow": "in" if net > 0 else ("out" if net < 0 else "flat"),
        })
    return out


def ticker(conn: sqlite3.Connection, *, limit: int = 10,
           squad: list[int] | None = None,
           now: dt.datetime | None = None) -> Ticker:
    """Build the Transfer Market Ticker for the Command Center.

    `squad` narrows the two owned-player lists, which are the actionable half:
    a rise you do not own is an opportunity, a fall you *do* own is money
    leaving your team value tonight.
    """
    forecasts = forecast(conn, now=now, persist=False)
    if not forecasts:
        return Ticker()

    live = [f for f in forecasts if not f.locked]
    # Ranked on `progress`, not probability: the logistic saturates at 1.0 for
    # everything well past the threshold, which ties the whole leaderboard on a
    # single-snapshot basis. Progress stays ordered however far past it goes.
    rising = sorted((f for f in live if f.p_rise > 0),
                    key=lambda f: f.progress, reverse=True)
    falling = sorted((f for f in live if f.p_fall > 0),
                     key=lambda f: f.progress)

    owned = set(squad or [])
    basis = (BASIS_TIME_SERIES
             if any(f.basis == BASIS_TIME_SERIES for f in forecasts)
             else (BASIS_EVENT_TOTAL
                   if any(f.basis == BASIS_EVENT_TOTAL for f in forecasts)
                   else BASIS_NONE))

    return Ticker(
        rising=rising[:limit],
        falling=falling[:limit],
        migration=ownership_migration(forecasts),
        hours_to_change=round(hours_to_next_change(now), 1),
        basis=basis,
        threshold=calibrate_threshold(conn),
        owned_rising=[f for f in rising if f.player_id in owned][:limit],
        owned_falling=[f for f in falling if f.player_id in owned][:limit],
    )
