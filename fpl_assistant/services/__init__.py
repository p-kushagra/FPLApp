"""Service layer: UI-agnostic view-models (ADR-001).

Pages call exactly one `build()` and render what comes back. Nothing here
imports Streamlit, so these are unit-testable without a browser and a future
HTTP adapter is an addition rather than a rewrite.
"""
from __future__ import annotations

from . import command_center, degrade, gw_summary
from .degrade import DataQuality, collect

__all__ = ["DataQuality", "collect", "command_center", "degrade", "gw_summary"]
