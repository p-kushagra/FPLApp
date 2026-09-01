# run_v2.ps1 - Launch the FPL Squad Assistant.
#
#   .\run_v2.ps1            # just launch
#   .\run_v2.ps1 -Ingest    # ingest + xP + pre-deadline freeze + calibration first
#
# One Streamlit multipage app on localhost. Refresh_Config.py is the control
# panel (data refresh + background scheduler); the four decision pages and the
# gameweek retrospective live under pages/.
param([switch]$Ingest)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { $venvPython = "python" }

if ($Ingest) {
    Write-Host "Refreshing data (ingest + xP + freeze + calibrate)..." -ForegroundColor Cyan
    & $venvPython -m fpl_assistant.ingest --all
}

Write-Host "Launching FPL Squad Assistant on http://localhost:8501" -ForegroundColor Green
& $venvPython -m streamlit run Refresh_Config.py
