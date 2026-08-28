# FPL Squad Assistant - run helper (Windows PowerShell)
# Usage:
#   .\run.ps1            # launch the dashboard
#   .\run.ps1 -Ingest    # refresh all data first, then launch
param([switch]$Ingest)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from template - set FPL_TEAM_ID inside it." -ForegroundColor Yellow
}

if ($Ingest) {
    python -m fpl_assistant.ingest --all
}

streamlit run app.py
