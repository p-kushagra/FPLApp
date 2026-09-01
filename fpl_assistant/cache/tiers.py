"""Cache TTL tiers.

`soft_ttl` triggers a background revalidation while the cached value is still
served. `hard_ttl` forces a blocking fetch. A tier with `frozen=True` is written
once and never revalidated -- used for facts that cannot change after the moment
they are recorded (a finished match, a rival's squad after the deadline).

Design doc section 5.2.
"""
from __future__ import annotations

from dataclasses import dataclass

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@dataclass(frozen=True)
class Tier:
    name: str
    soft_ttl: int          # seconds until a background refresh is wanted
    hard_ttl: int          # seconds until a blocking refresh is required
    frozen: bool = False   # write-once; never revalidate
    note: str = ""

    def __post_init__(self) -> None:
        if not self.frozen and self.hard_ttl < self.soft_ttl:
            raise ValueError(
                f"tier {self.name!r}: hard_ttl must be >= soft_ttl "
                f"({self.hard_ttl} < {self.soft_ttl})"
            )


_INF = 10 * 365 * DAY  # "never expires" without special-casing None everywhere

TIERS: dict[str, Tier] = {
    "fpl_static": Tier("fpl_static", 24 * HOUR, 72 * HOUR,
                       note="player metadata and team strengths move slowly"),
    "fpl_fixtures": Tier("fpl_fixtures", 6 * HOUR, 24 * HOUR,
                         note="rearrangements land unpredictably; cheap to refetch"),
    "fpl_live": Tier("fpl_live", 60, 5 * MINUTE,
                     note="live scoring, bounded by the 60s poll"),
    "fpl_prices": Tier("fpl_prices", 1 * HOUR, 6 * HOUR,
                       note="feeds the price model; needs intra-day granularity"),
    "fpl_entry": Tier("fpl_entry", 15 * MINUTE, 6 * HOUR,
                      note="own team, pre-deadline"),
    "ml_standings": Tier("ml_standings", 1 * HOUR, 12 * HOUR,
                         note="league table position"),
    "ml_picks": Tier("ml_picks", _INF, _INF, frozen=True,
                     note="ADR-005: immutable once frozen at the deadline"),
    "understat_player": Tier("understat_player", 6 * HOUR, 7 * DAY,
                             note="enrichment; a week-old xG rate still informs"),
    "understat_league": Tier("understat_league", 6 * HOUR, 7 * DAY,
                             note="bulk season table, one request"),
    "understat_match": Tier("understat_match", _INF, _INF, frozen=True,
                            note="a finished match never changes"),
    "news_rss": Tier("news_rss", 30 * MINUTE, 6 * HOUR,
                     note="unchanged from v1"),
}


def get_tier(name: str) -> Tier:
    try:
        return TIERS[name]
    except KeyError:
        raise KeyError(
            f"unknown cache tier {name!r}; known tiers: {sorted(TIERS)}"
        ) from None
