# FPL Squad Assistant - run helper (Windows PowerShell)
#
# Usage:
#   .\run.ps1            # launch the dashboard
#   .\run.ps1 -Ingest    # refresh all data first, then launch
#
# If PowerShell blocks this script, run it as:
#   powershell -ExecutionPolicy Bypass -File .\run.ps1 -Ingest
param([switch]$Ingest)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- locate a usable Python -------------------------------------------------
# On Windows without Python installed, bare `python` launches the Microsoft Store
# stub and then fails confusingly. Resolve a real 3.11+ interpreter first.
$python = $null
foreach ($candidate in @("py", "python3", "python")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like "*WindowsApps*") { continue }   # Microsoft Store stub
    try {
        $v = & $candidate --version 2>&1
        if ($v -match "Python (\d+)\.(\d+)" -and
            [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) {
            $python = $candidate
            break
        }
    } catch { continue }
}

if (-not $python) {
    Write-Host "Python 3.11+ was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ and tick"
    Write-Host "'Add python.exe to PATH' during setup, then re-run this script."
    Write-Host "Use a python.org build - it includes the SQLite FTS5 module that"
    Write-Host "the news search depends on."
    exit 1
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment using '$python'..."
    & $python -m venv .venv
}

# Call the venv interpreter directly rather than activating, so this script does
# not also depend on Activate.ps1 being permitted by the execution policy.
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

& $venvPython -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "This Python was built without SQLite FTS5, which the news search needs." -ForegroundColor Red
    Write-Host "Install a standard build from https://www.python.org/downloads/,"
    Write-Host "delete the .venv folder, and re-run."
    exit 1
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env - set FPL_TEAM_ID inside it." -ForegroundColor Yellow
}

$match = Select-String -Path ".env" -Pattern "^FPL_TEAM_ID=(\d+)" -ErrorAction SilentlyContinue
if (-not $match) {
    Write-Host "FPL_TEAM_ID is not set in .env - squad pages stay empty until it is." -ForegroundColor Yellow
    Write-Host "Find it in your FPL Points page URL: /entry/<THIS_NUMBER>/event/1"
}

if ($Ingest) {
    & $venvPython -m fpl_assistant.ingest --all
}

& $venvPython -m streamlit run app.py
