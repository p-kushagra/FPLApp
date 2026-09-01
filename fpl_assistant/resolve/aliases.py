"""Loader for `config/aliases.yaml`.

The resolver reads this file but never writes it. Low-confidence candidates go
to `entity_map` with status='unresolved' for the operator to review; a binding
only becomes permanent when a human puts it here.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = PROJECT_ROOT / "config" / "aliases.yaml"


@lru_cache(maxsize=4)
def _load(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def clear_cache() -> None:
    _load.cache_clear()


def overrides(path: Path | None = None) -> dict[int, str]:
    """{fpl_player_id: understat_id} from the manual override list."""
    data = _load(str(path or DEFAULT_PATH))
    out: dict[int, str] = {}
    for row in data.get("overrides") or []:
        if not isinstance(row, dict):
            continue
        fpl_id, us_id = row.get("fpl_id"), row.get("understat_id")
        if fpl_id is not None and us_id is not None:
            out[int(fpl_id)] = str(us_id)
    return out


def team_aliases(path: Path | None = None) -> dict[str, str]:
    """{understat team_title: FPL short_name}."""
    data = _load(str(path or DEFAULT_PATH))
    return {str(k): str(v) for k, v in (data.get("team_aliases") or {}).items()}


def missing_team_aliases(conn: sqlite3.Connection, season: int,
                         path: Path | None = None) -> list[str]:
    """Understat clubs with no FPL mapping.

    Club scoping is what makes resolution deterministic, so an unmapped club
    silently empties its candidate set and every one of its players goes
    unresolved. Surfacing the gap turns a mystery into a one-line config fix.
    """
    known = set(team_aliases(path))
    seen = {
        r["team_title"] for r in conn.execute(
            "SELECT DISTINCT team_title FROM understat_player WHERE season = ?",
            (season,),
        ) if r["team_title"]
    }
    return sorted(seen - known)
