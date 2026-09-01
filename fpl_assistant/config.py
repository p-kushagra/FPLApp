"""Portable configuration. All paths resolve relative to the project root unless
overridden via environment variables, so the project moves cleanly between devices."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _resolve(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass
class Config:
    data_dir: Path
    db_path: Path
    fpl_team_id: int | None
    top_managers_sample: int
    news_recency_days: int
    insights_provider: str
    claude_mode: str
    claude_cli_path: str
    briefings_dir: Path
    exports_dir: Path
    sources: dict = field(default_factory=dict)
    calendar: dict = field(default_factory=dict)
    regions: dict = field(default_factory=dict)
    managers: dict = field(default_factory=dict)
    references: dict = field(default_factory=dict)
    leagues: dict = field(default_factory=dict)

    @property
    def default_rival_count(self) -> int:
        return int(self.leagues.get("default_rival_count") or 8)

    @property
    def max_rivals(self) -> int:
        """Caps the per-gameweek freeze request budget."""
        return int(self.leagues.get("max_rivals") or 20)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_config() -> Config:
    data_dir = _resolve(_get("DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    db_path = _resolve(_get("DB_PATH", str(data_dir / "fpl.sqlite")))
    briefings_dir = _resolve(_get("BRIEFINGS_DIR", "briefings"))
    exports_dir = _resolve(_get("EXPORTS_DIR", "exports"))
    briefings_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    sources_path = _resolve(_get("SOURCES_PATH", "config/sources.yaml"))
    sources = _load_yaml(sources_path)

    calendar = _load_yaml(_resolve(_get("CALENDAR_PATH", "config/calendar.yaml")))
    regions_raw = _load_yaml(_resolve(_get("REGIONS_PATH", "config/regions.yaml")))
    regions = {int(k): v for k, v in (regions_raw.get("regions") or {}).items()}
    managers = _load_yaml(_resolve(_get("MANAGERS_PATH", "config/managers.yaml")))
    references = _load_yaml(_resolve(_get("REFERENCES_PATH", "config/references.yaml")))
    leagues = _load_yaml(_resolve(_get("LEAGUES_PATH", "config/leagues.yaml")))

    team_id = _get("FPL_TEAM_ID")

    return Config(
        data_dir=data_dir,
        db_path=db_path,
        fpl_team_id=int(team_id) if team_id and team_id.isdigit() else None,
        top_managers_sample=int(_get("TOP_MANAGERS_SAMPLE", "50")),
        news_recency_days=int(_get("NEWS_RECENCY_DAYS", "10")),
        insights_provider=_get("INSIGHTS_PROVIDER", "null"),
        claude_mode=_get("CLAUDE_MODE", "bundle"),
        claude_cli_path=_get("CLAUDE_CLI_PATH", "claude"),
        briefings_dir=briefings_dir,
        exports_dir=exports_dir,
        sources=sources,
        calendar=calendar,
        regions=regions,
        managers=managers,
        references=references,
        leagues=leagues,
    )
