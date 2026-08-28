#!/usr/bin/env bash
# Weekly refresh for the FPL Squad Assistant (macOS / Linux).
#
# Usage:
#   ./scripts/weekly_refresh.sh
#   ./scripts/weekly_refresh.sh --install-cron   # every Tuesday at 08:00
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" = "--install-cron" ]; then
    LINE="0 8 * * 2 cd $ROOT && ./scripts/weekly_refresh.sh >> $ROOT/logs/cron.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v 'fpl_assistant.ingest' ; echo "$LINE" ) | crontab -
    echo "Installed weekly cron job (Tuesdays 08:00)."
    exit 0
fi

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

mkdir -p "$ROOT/logs"
LOG="$ROOT/logs/refresh-$(date +%F).log"

{
    echo "=== FPL weekly refresh $(date '+%F %H:%M') ==="
    "$PYTHON" -m fpl_assistant.ingest --all
    "$PYTHON" -m fpl_assistant.check_sources
} 2>&1 | tee "$LOG"

echo "Done. Log: $LOG"
