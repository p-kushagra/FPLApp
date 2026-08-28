"""Config staleness tracking.

Manual configs (European qualifiers, managers, cup dates) go stale silently and
that is exactly how wrong data creeps back in. This reads config/references.yaml
and reports which entries are overdue for a review.
"""
from __future__ import annotations

import datetime as dt

from .config import Config


def _parse_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def manual_sources(cfg: Config, today: dt.date | None = None) -> list[dict]:
    """Every manually maintained config with its age and review status."""
    today = today or dt.date.today()
    out = []
    for entry in (cfg.references or {}).get("manual") or []:
        verified = _parse_date(entry.get("last_verified"))
        every = int(entry.get("review_every_days", 30))
        age = (today - verified).days if verified else None
        due_in = (every - age) if age is not None else None

        if age is None:
            status = "unknown"
        elif age >= every * 2:
            status = "overdue"
        elif age >= every:
            status = "due"
        else:
            status = "ok"

        out.append({
            "key": entry.get("key", "?"),
            "name": entry.get("name", entry.get("key", "?")),
            "config_file": entry.get("config_file", ""),
            "last_verified": verified.isoformat() if verified else "never",
            "age_days": age,
            "review_every_days": every,
            "due_in_days": due_in,
            "status": status,
            "sources": entry.get("sources") or [],
            "check": (entry.get("check") or "").strip(),
        })
    out.sort(key=lambda e: (e["status"] != "overdue", e["status"] != "due",
                            -(e["age_days"] or 0)))
    return out


def stale_sources(cfg: Config, today: dt.date | None = None) -> list[dict]:
    return [s for s in manual_sources(cfg, today) if s["status"] in ("due", "overdue", "unknown")]


def refresh_prompt(cfg: Config, today: dt.date | None = None) -> str:
    """Build the briefing an agent needs to re-verify the stale configs."""
    stale = stale_sources(cfg, today)
    if not stale:
        return "All manual configs are within their review window. Nothing to do."

    lines = [
        "Apply the `fpl-config-refresh` skill in .claude/skills/ of this repo.",
        "Re-verify the configs below against the listed sources and report ONLY the",
        "changes needed, as exact YAML edits. Do not restate unchanged entries.",
        "",
    ]
    for s in stale:
        lines.append(f"### {s['name']}  [{s['status']}]")
        lines.append(f"- file: {s['config_file']} ({s.get('key')})")
        lines.append(f"- last verified: {s['last_verified']} ({s['age_days']} days ago, "
                     f"review every {s['review_every_days']})")
        if s["check"]:
            lines.append(f"- what to check: {s['check']}")
        for url in s["sources"]:
            lines.append(f"- source: {url}")
        lines.append("")
    return "\n".join(lines)
