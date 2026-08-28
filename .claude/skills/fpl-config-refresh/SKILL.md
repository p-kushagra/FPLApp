---
name: fpl-config-refresh
description: >
  Re-verify the FPL app's manually maintained config against its recorded sources —
  European qualifiers, Premier League managers, domestic cup round dates,
  international break and tournament windows, and RSS feed health. Use when asked to
  refresh config, check what is stale, update the calendar or managers file, or when
  handed a config-refresh briefing. Triggers: refresh config, update calendar, check
  sources, stale, verify qualifiers, manager change, cup dates, weekly refresh.
---

# FPL Config Refresh

Keep the hand-maintained YAML accurate. `config/references.yaml` is the registry:
it lists every manual config, where it came from, and how often to re-check it.

## Why any of this is manual

Verified 2026-08-28 — these are not laziness, they are real gaps:

- The **FPL API has no manager data** (`element_types` 1–4 only, no manager field
  on teams).
- The **Premier League Pulse `/staff` endpoint returns 404** for all 20 clubs.
- The **FPL API carries no cup fixtures**, only league fixtures.
- **European qualifiers** are not in any free machine-readable feed.

## Workflow

1. Run `python -m fpl_assistant.check_sources` (or read the supplied briefing) to
   see which entries are `due` or `overdue`.
2. For each stale entry, open the URLs in its `sources` list and check the specific
   question in its `check` field.
3. Report **only what changed**, as exact YAML edits. Do not restate unchanged
   entries — that wastes tokens and hides the real change.
4. Update `last_verified` in **both** the config file and `config/references.yaml`.

## Verification rules — these matter

1. **Cite the source for every change.** A club name with no citation does not go
   into the config.
2. **Prefer the primary source.** UEFA.com over Wikipedia; the club's own
   announcement over a news aggregator.
3. **Never guess to fill a gap.** If a fact cannot be verified, leave the field
   empty and say so. An empty `teams` list produces no rotation flag; a wrong one
   silently corrupts every rotation score.
4. **Watch the `all_clubs` semantics.** In `club_competitions`, an empty `teams`
   list with no `all_clubs: true` means "unknown, do not flag". Only domestic cups
   should carry `all_clubs: true`.
5. **Reject hedged answers.** If a source says "projected", "expected" or
   "likely", it is not verification. This exact failure put a wrong manager list
   into the repo once already.

## What changes, and when

| Config | Typically changes | Watch for |
|---|---|---|
| European qualifiers | Whenever a club is eliminated | Knockout rounds Feb–May; remove eliminated clubs so their midweek load stops |
| Managers | Any time | Sackings cluster around October and after a bad run |
| Cup rounds | A few times a season | Round dates confirmed only weeks ahead |
| Tournaments | Rarely | AFCON and Copa windows; confirm well before players leave |
| RSS feeds | Rarely | Persistent non-200 in the ingest warnings |
| Region ids | Rarely | Unmapped ids for CAF/CONMEBOL nations before a tournament |

## Output format

Be terse. For each change:

> **`config/calendar.yaml` → UEFA Europa League**
> Remove `SUN` — Sunderland eliminated in the knockout play-off (uefa.com, 2027-02-20).
> Update `last_verified: 2027-02-21`.

If nothing changed, say so in one line and update `last_verified` anyway — a
confirmed no-change is still a verification.
