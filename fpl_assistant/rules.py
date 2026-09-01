"""Loader for `config/rules.yaml`.

A separate module rather than a field on `Config` so that anything needing the
rules -- the solver, the FT bank, the xP engine -- can read them without
constructing an app config, and so tests can inject a variant ruleset without
touching the environment.

FPL has already changed the free-transfer cap once (2 -> 5) and added an entire
scoring category (defensive contribution). Every one of those numbers lives in
YAML precisely so a rule change is a config edit, never a code change.
"""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / "config" / "rules.yaml"

# Used only if config/rules.yaml is missing entirely. Keeps the engine runnable
# on a fresh checkout; the real values are the YAML's.
_FALLBACK: dict[str, Any] = {
    "transfers": {
        "free_per_gw": 1, "max_banked": 5, "hit_cost": 4,
        "chip_retains_ft": True, "chip_accrues_ft": False,
    },
    "squad": {
        "size": 15, "budget": 100.0, "max_per_club": 3,
        "quota": {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3},
        "formation": {"GKP": [1, 1], "DEF": [3, 5], "MID": [2, 5], "FWD": [1, 3]},
        "sell_price_profit_share": 0.5,
    },
    "scoring": {
        "appearance": {"under_60": 1, "over_60": 2},
        "goal": {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
        "assist": 3,
        "clean_sheet": {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
        "saves_per_3": 1, "yellow": -1, "red": -3,
        "defensive_contribution": {
            "points": 2, "threshold": {"DEF": 10, "MID": 12, "FWD": 12},
        },
    },
    "chips": {
        "available": ["wildcard", "bench_boost", "triple_captain", "free_hit"],
        "wildcards_per_season": 2, "second_half_start_gw": 20,
    },
}


@lru_cache(maxsize=4)
def _load_cached(path_str: str) -> str:
    return Path(path_str).read_text(encoding="utf-8")


def load_rules(path: Path | None = None, *, use_cache: bool = True) -> dict[str, Any]:
    """Return the ruleset. Always a deep copy, so callers cannot mutate it."""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return copy.deepcopy(_FALLBACK)

    raw = _load_cached(str(target)) if use_cache else target.read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw) or {}

    merged = copy.deepcopy(_FALLBACK)
    for section, values in loaded.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def clear_cache() -> None:
    _load_cached.cache_clear()


# -- convenience accessors, so callers do not index dicts by hand -----------
def transfers(rules: dict | None = None) -> dict:
    return (rules or load_rules())["transfers"]


def squad(rules: dict | None = None) -> dict:
    return (rules or load_rules())["squad"]


def scoring(rules: dict | None = None) -> dict:
    return (rules or load_rules())["scoring"]


POSITIONS = ("GKP", "DEF", "MID", "FWD")
ELEMENT_TYPE_TO_POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
