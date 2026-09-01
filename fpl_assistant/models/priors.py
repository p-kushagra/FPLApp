"""Player-level priors and Bayesian sample-size blending.

Early in a season the xP engine has almost nothing to stand on: two gameweeks
of per-90 rates are dominated by variance, and 250+ players have no minutes at
all. This module supplies the missing evidence in three layers, weakest claim
last:

1. **Historical baseline** - the player's own last-season rates from
   `historical_player_baselines` (seeded by `scripts/seed_history.py` from
   FPL `history_past`, with true npxG from Understat where the player is
   resolved).
2. **Championship translation** - underlying rates earned in the Championship
   are multiplied by 0.68 before use. Promoted attackers historically retain
   about two-thirds of their underlying output in the top flight; applying the
   haircut at *read* time keeps the stored row a faithful record of the source.
3. **Imputed matrix** - players with no usable history anywhere get an
   empirical prior conditioned on position and FPL launch price. FPL's own
   pricing is a strong summary of the market's expectation, which is exactly
   what a prior is supposed to encode.

The blend itself is deliberately the simplest thing that fixes the N=2
distortion, a linear credibility ramp:

    weight          = min(1, current_season_minutes / 720)
    rate_effective  = weight * rate_current + (1 - weight) * rate_prior

720 minutes = eight full matches: by then the current season outweighs the
prior entirely, and at zero minutes the projection is 100% prior. The read
path returns None rather than an imputed guess when the table has not been
seeded, so an unseeded database falls back to the v2 positional-shrinkage
behaviour instead of silently changing every projection.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Minutes of current-season evidence at which the prior's weight reaches zero.
BLEND_MINUTES = 720.0

# Translation multiplier for underlying rates earned in the Championship.
CHAMPIONSHIP_HAIRCUT = 0.68

# A historical season with fewer minutes than this says more about absence
# than about rates; it is skipped in favour of an older season or the matrix.
MIN_PRIOR_MINUTES = 450.0

SOURCE_HISTORY = "fpl_history"
SOURCE_UNDERSTAT = "understat"
SOURCE_IMPUTED = "imputed"

COMP_PL = "PL"
COMP_CHAMPIONSHIP = "CHAMPIONSHIP"


@dataclass(frozen=True)
class PlayerPrior:
    """Per-90 baseline rates for one player, translation already applied."""

    npxg90: float
    xa90: float
    xcs_rate: float      # clean sheets per 90 played (team-context base rate)
    defcon90: float      # defensive-contribution actions per 90
    minutes: float       # minutes behind the estimate (0 for imputed)
    source: str
    season: str


# --------------------------------------------------------------------------
# Imputation matrix: (position, price_lo, price_hi] in GBPm.
#
# The three promoted-asset rows are the calibrated cells (design: promoted
# DEF GBP4.0-4.5m 0.02 npxG90 / 0.04 xA90 / 16% base xCS; promoted MID
# GBP5.0-5.5m 0.05/0.08/16%; promoted FWD GBP5.5-6.0m 0.28/0.12). The
# remaining rows extend the same price-conditioned logic across the full
# pricing range so every zero-history asset gets *some* defensible prior --
# an £8m signing from abroad is not a £4.5m promoted centre-back.
# --------------------------------------------------------------------------
_MATRIX: list[tuple[str, float, float, float, float, float, float]] = [
    # pos    lo    hi    npxg90  xa90  xcs   defcon90
    ("GKP", 0.0, 4.75, 0.00, 0.01, 0.16, 0.2),
    ("GKP", 4.75, 99.0, 0.00, 0.01, 0.26, 0.2),
    ("DEF", 0.0, 4.55, 0.02, 0.04, 0.16, 8.0),   # promoted DEF cell
    ("DEF", 4.55, 5.55, 0.05, 0.08, 0.22, 7.5),
    ("DEF", 5.55, 99.0, 0.10, 0.15, 0.28, 6.5),
    ("MID", 0.0, 5.05, 0.04, 0.06, 0.16, 6.0),
    ("MID", 5.05, 5.55, 0.05, 0.08, 0.16, 6.0),  # promoted MID cell
    ("MID", 5.55, 6.55, 0.15, 0.15, 0.20, 5.0),
    ("MID", 6.55, 99.0, 0.25, 0.22, 0.25, 4.0),
    ("FWD", 0.0, 5.55, 0.20, 0.08, 0.16, 3.0),
    ("FWD", 5.55, 6.05, 0.28, 0.12, 0.16, 3.0),  # promoted FWD cell
    ("FWD", 6.05, 7.55, 0.35, 0.15, 0.20, 3.0),
    ("FWD", 7.55, 99.0, 0.45, 0.18, 0.25, 3.0),
]

_MATRIX_FALLBACK = {"GKP": 0, "DEF": 2, "MID": 5, "FWD": 9}  # cheapest row per pos


def imputed_prior(position: str, price_m: float | None) -> PlayerPrior:
    """Empirical prior for a zero-history asset, from position and price."""
    pos = position if position in _MATRIX_FALLBACK else "MID"
    price = float(price_m) if price_m else 0.0
    row = None
    for cand in _MATRIX:
        if cand[0] == pos and cand[1] < price <= cand[2]:
            row = cand
            break
    if row is None:
        row = _MATRIX[_MATRIX_FALLBACK[pos]]
    _, _, _, npxg, xa, xcs, dc = row
    return PlayerPrior(npxg90=npxg, xa90=xa, xcs_rate=xcs, defcon90=dc,
                       minutes=0.0, source=SOURCE_IMPUTED, season="imputed")


def translate(rate: float | None, competition: str | None) -> float:
    """Haircut underlying rates earned below the Premier League."""
    value = float(rate or 0.0)
    if (competition or COMP_PL).upper() == COMP_CHAMPIONSHIP:
        return value * CHAMPIONSHIP_HAIRCUT
    return value


# --------------------------------------------------------------------------
# Blending
# --------------------------------------------------------------------------
def blend_weight(current_minutes: float | None) -> float:
    """Credibility of the current season: 0 at no minutes, 1 at 720+."""
    return min(1.0, max(0.0, float(current_minutes or 0.0)) / BLEND_MINUTES)


def blend(rate_current: float, current_minutes: float | None,
          rate_prior: float) -> float:
    """weight * current + (1 - weight) * prior. Zero minutes => pure prior."""
    w = blend_weight(current_minutes)
    return w * float(rate_current) + (1.0 - w) * float(rate_prior)


# --------------------------------------------------------------------------
# Read path
# --------------------------------------------------------------------------
def _row_to_prior(row: sqlite3.Row) -> PlayerPrior:
    comp = row["competition"]
    return PlayerPrior(
        npxg90=translate(row["npxg90_prior"], comp),
        xa90=translate(row["xa90_prior"], comp),
        # Clean-sheet and DefCon rates are outcome frequencies, not underlying
        # chance creation; the finishing-environment haircut does not apply.
        xcs_rate=float(row["xcs_rate_prior"] or 0.0),
        defcon90=float(row["defcon_rate_prior"] or 0.0),
        minutes=float(row["total_minutes"] or 0.0),
        source=str(row["source"]),
        season=str(row["season_name"]),
    )


def player_prior(conn: sqlite3.Connection, player_id: int) -> PlayerPrior | None:
    """Best available baseline for one player, or None if never seeded.

    Preference order: the most recent real season with enough minutes
    (Understat beats FPL history inside a season, because its npxG is truly
    the penalty-free rate the model wants), then the imputed matrix row written at
    seed time. None means the seeding pipeline has not run for this player,
    and the caller should keep the legacy positional-shrinkage behaviour.
    """
    rows = conn.execute(
        """SELECT * FROM historical_player_baselines WHERE player_id = ?
           ORDER BY season_name DESC,
                    CASE source WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END""",
        (player_id, SOURCE_UNDERSTAT, SOURCE_HISTORY),
    ).fetchall()
    return _pick(rows)


def _pick(rows: list[sqlite3.Row]) -> PlayerPrior | None:
    imputed = None
    for row in rows:
        if row["source"] == SOURCE_IMPUTED:
            imputed = imputed or row
            continue
        if float(row["total_minutes"] or 0.0) >= MIN_PRIOR_MINUTES:
            return _row_to_prior(row)
    return _row_to_prior(imputed) if imputed is not None else None


def load_priors(conn: sqlite3.Connection) -> dict[int, PlayerPrior]:
    """All player priors in one query, for the projection loop."""
    try:
        rows = conn.execute(
            """SELECT * FROM historical_player_baselines
               ORDER BY player_id, season_name DESC,
                        CASE source WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END""",
            (SOURCE_UNDERSTAT, SOURCE_HISTORY),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # pre-v4 database: behave exactly like v2

    by_player: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_player.setdefault(int(row["player_id"]), []).append(row)

    out: dict[int, PlayerPrior] = {}
    for pid, player_rows in by_player.items():
        prior = _pick(player_rows)
        if prior is not None:
            out[pid] = prior
    return out


def current_season_minutes(conn: sqlite3.Connection,
                           latest_gw: int) -> dict[int, float]:
    """Raw (unweighted) minutes played this season up to `latest_gw`.

    This is the credibility denominator for `blend_weight`. It is deliberately
    *not* the recency-weighted minutes the rate estimators use: credibility is
    about how much evidence exists, and discounting old evidence there would
    keep the prior alive deep into the season.
    """
    return {int(r["player_id"]): float(r["m"] or 0.0) for r in conn.execute(
        """SELECT player_id, SUM(minutes) m FROM player_gw
           WHERE gw <= ? GROUP BY player_id""", (latest_gw,))}
