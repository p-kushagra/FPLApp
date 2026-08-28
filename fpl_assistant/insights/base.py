"""Insight data model and provider protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Insight:
    player_id: int
    signal_type: str          # injury | illness | rotation | suspension | fit | none | unknown
    status: str               # short human status, e.g. "Doubt - knock"
    expected_return: str       # e.g. "GW7" or ""
    confidence: str            # low | medium | high
    summary: str
    source_urls: str
    provider: str


@runtime_checkable
class InsightsProvider(Protocol):
    def summarise(self, player: dict, chunks: list[dict]) -> Insight:
        ...
