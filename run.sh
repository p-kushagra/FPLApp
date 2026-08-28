#!/usr/bin/env bash
# FPL Squad Assistant - run helper (macOS / Linux)
# Usage:
#   ./run.sh            # launch the dashboard
#   ./run.sh --ingest   # refresh all data first, then launch
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Created .env from template - set FPL_TEAM_ID inside it."
fi

if [ "${1:-}" = "--ingest" ]; then
    python -m fpl_assistant.ingest --all
fi

streamlit run app.py
