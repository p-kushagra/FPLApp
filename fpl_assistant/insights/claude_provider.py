"""Insight provider backed by a personal Claude subscription (no metered API).

Two modes:
  bundle - write a briefing file for you to run through Claude on your VM, then drop
           the returned JSON into the exports/ folder to import it.
  cli    - invoke the `claude` CLI non-interactively on this machine and parse the JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess

from ..config import Config
from .base import Insight

SCHEMA_HINT = (
    '{"player_id": <int>, "signal_type": "injury|illness|rotation|suspension|fit|none", '
    '"status": "<short status>", "expected_return": "<gw or empty>", '
    '"confidence": "low|medium|high", "summary": "<2-3 sentences, cite dates>", '
    '"sources": ["<url>", "..."]}'
)


def build_prompt(player: dict, chunks: list[dict]) -> str:
    team = player.get("team_name") or player.get("team_short") or ""
    lines = [
        f"You are an FPL analyst. Assess availability for {player['web_name']} "
        f"(id {player['id']}, {team}) for the upcoming gameweek.",
        "Use ONLY the sources below. Cite dates. Do not speculate beyond the text.",
        "Respond with a SINGLE JSON object and nothing else, matching this shape:",
        SCHEMA_HINT,
        "",
        "SOURCES:",
    ]
    if not chunks:
        lines.append("- (no recent tagged news found)")
    for c in chunks:
        lines.append(f"- [{c.get('published_at')}] {c.get('source')}: {c['text']} "
                     f"({c.get('url')})")
    return "\n".join(lines)


def _parse_json(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


class ClaudeSubscriptionProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def summarise(self, player: dict, chunks: list[dict]) -> Insight:
        prompt = build_prompt(player, chunks)
        if self.cfg.claude_mode == "cli":
            return self._via_cli(player, chunks, prompt)
        return self._via_bundle(player, chunks, prompt)

    # -- bundle mode -------------------------------------------------------
    def _via_bundle(self, player: dict, chunks: list[dict], prompt: str) -> Insight:
        stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", player["web_name"])
        path = self.cfg.briefings_dir / f"{safe}-{player['id']}-{stamp}.md"
        path.write_text(prompt, encoding="utf-8")
        sources = ", ".join(sorted({c.get("url", "") for c in chunks if c.get("url")}))
        return Insight(
            player_id=player["id"],
            signal_type="pending",
            status="Briefing written — run through Claude, then import the JSON",
            expected_return="",
            confidence="",
            summary=f"Briefing saved to {path}. After Claude answers, save the JSON to "
                    f"{self.cfg.exports_dir} and click Import.",
            source_urls=sources,
            provider="claude:bundle",
        )

    # -- cli mode ----------------------------------------------------------
    def _via_cli(self, player: dict, chunks: list[dict], prompt: str) -> Insight:
        try:
            result = subprocess.run(
                [self.cfg.claude_cli_path, "-p", prompt],
                capture_output=True, text=True, timeout=180,
            )
            data = _parse_json(result.stdout) or {}
        except (OSError, subprocess.SubprocessError) as exc:
            return Insight(
                player_id=player["id"], signal_type="error",
                status="Claude CLI call failed",
                expected_return="", confidence="",
                summary=f"Could not run Claude CLI: {exc}. Check CLAUDE_CLI_PATH or use bundle mode.",
                source_urls="", provider="claude:cli",
            )
        sources = data.get("sources")
        if isinstance(sources, list):
            sources = ", ".join(sources)
        return Insight(
            player_id=player["id"],
            signal_type=data.get("signal_type", "unknown"),
            status=data.get("status", ""),
            expected_return=data.get("expected_return", ""),
            confidence=data.get("confidence", ""),
            summary=data.get("summary", result.stdout.strip()[:500]),
            source_urls=sources or "",
            provider="claude:cli",
        )
