"""Source adapters. Everything that talks to the outside world lives here.

The public surface is deliberately narrow: import the envelope from here, and
the adapters from their own modules.
"""
from __future__ import annotations

from .base import (
    Malformed,
    NotFound,
    Quality,
    RateLimited,
    SourceError,
    SourceResult,
    Unavailable,
)

__all__ = [
    "Malformed",
    "NotFound",
    "Quality",
    "RateLimited",
    "SourceError",
    "SourceResult",
    "Unavailable",
]
