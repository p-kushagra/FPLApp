"""FPL <-> Understat entity resolution."""
from __future__ import annotations

from .matcher import (
    MatchCandidate,
    Resolution,
    ResolveReport,
    normalise_name,
    resolve_all,
    resolve_one,
    unresolved,
)

__all__ = [
    "MatchCandidate",
    "Resolution",
    "ResolveReport",
    "normalise_name",
    "resolve_all",
    "resolve_one",
    "unresolved",
]
