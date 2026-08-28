"""Rule-based provider used when no AI is configured (fully offline, £0)."""
from __future__ import annotations

from .base import Insight

_DOUBT_WORDS = ("injury", "injured", "doubt", "knock", "illness", "ill", "setback", "scan")
_GOOD_WORDS = ("returns", "return", "fit", "available", "back in training", "trained")


def _classify(player: dict, chunks: list[dict]) -> tuple[str, str, str]:
    status = player.get("status") or "a"
    chance = player.get("chance_of_playing_next_round")
    if status in ("i", "u"):
        return "injury", "Ruled out per FPL status", "high"
    if status == "s":
        return "suspension", "Suspended per FPL status", "high"
    if status == "d" or (chance is not None and chance < 100):
        pct = f" ({chance}% chance)" if chance is not None else ""
        return "injury", f"Doubtful per FPL status{pct}", "medium"

    text = " ".join(c["text"].lower() for c in chunks)
    if any(w in text for w in _DOUBT_WORDS):
        return "injury", "Possible fitness concern in recent news", "low"
    if any(w in text for w in _GOOD_WORDS):
        return "fit", "Recent news suggests available", "low"
    return "none", "No availability concerns detected", "low"


class NullProvider:
    def summarise(self, player: dict, chunks: list[dict]) -> Insight:
        signal, status, confidence = _classify(player, chunks)
        headlines = [c["text"][:160] for c in chunks[:3]]
        summary = " | ".join(headlines) if headlines else "No recent tagged news."
        if player.get("news"):
            summary = f"FPL note: {player['news']}. " + summary
        sources = ", ".join(sorted({c.get("url", "") for c in chunks if c.get("url")}))
        return Insight(
            player_id=player["id"],
            signal_type=signal,
            status=status,
            expected_return="",
            confidence=confidence,
            summary=summary,
            source_urls=sources,
            provider="null:rules",
        )
