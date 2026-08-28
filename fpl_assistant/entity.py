"""Link news chunks to FPL players by name (deterministic, no models)."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

_STOP_SURNAMES = {"silva", "sanchez", "santos", "pereira", "gomes", "fernandes"}


def build_alias_index(players: list[dict]) -> dict[str, list[int]]:
    """Map a lowercased alias -> list of player ids that share it."""
    index: dict[str, list[int]] = {}
    for p in players:
        aliases: set[str] = set()
        web = (p.get("web_name") or "").strip().lower()
        second = (p.get("second_name") or "").strip().lower()
        first = (p.get("first_name") or "").strip().lower()
        if web:
            aliases.add(web)
        if second:
            aliases.add(second)
        if first and second:
            aliases.add(f"{first} {second}")
        for alias in aliases:
            if len(alias) < 3:
                continue
            index.setdefault(alias, []).append(p["id"])
    return index


def tag_text(text: str, alias_index: dict[str, list[int]]) -> dict[int, float]:
    """Return player_id -> confidence score for players mentioned in the text."""
    lowered = (text or "").lower()
    if not lowered:
        return {}

    hits: dict[int, float] = {}
    for alias, player_ids in alias_index.items():
        multiword = " " in alias
        if not re.search(r"\b" + re.escape(alias) + r"\b", lowered):
            continue
        # Ambiguous single common surnames are down-weighted.
        if len(player_ids) > 1:
            base = 70.0
        elif not multiword and alias in _STOP_SURNAMES:
            base = 75.0
        elif multiword:
            base = 100.0
        else:
            base = 90.0
        for pid in player_ids:
            hits[pid] = max(hits.get(pid, 0.0), base)
    return hits


def fuzzy_contains(text: str, full_name: str, threshold: int = 90) -> bool:
    """Optional fuzzy fallback for near-miss spellings of a full name."""
    if not text or not full_name:
        return False
    return fuzz.partial_ratio(full_name.lower(), text.lower()) >= threshold
