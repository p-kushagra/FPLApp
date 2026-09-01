# run_v2.ps1 - Refresh data through the full v2 pipeline, then launch the app.
#
#   .\run_v2.ps1            # just launch
#   .\run_v2.ps1 -Ingest    # ingest + xP + pre-deadline freeze + calibration, then launch
#
# The app is one Streamlit multipage tree: Refresh_Config.py is the control
# panel, pages/0_Gameweek_Summary.py and pages/1_Command_Center.py are the two
# v2 decision pages.
param([switch]$Ingest)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if ($Ingest) {
    Write-Host "Refreshing data (ingest + xP + freeze + calibrate)..."
    & $venvPython -m fpl_assistant.ingest --all
}

Write-Host "Launching FPL Squad Assistant..."
& $venvPython -m streamlit run Refresh_Config.py
