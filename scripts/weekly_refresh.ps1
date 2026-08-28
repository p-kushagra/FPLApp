# Weekly refresh for the FPL Squad Assistant.
#
# Pulls everything the app can fetch automatically, then reports which manual
# configs (European qualifiers, managers, cup dates) are due for a review.
#
# Usage:
#   .\scripts\weekly_refresh.ps1
#   .\scripts\weekly_refresh.ps1 -Register     # install as a weekly scheduled task
#   .\scripts\weekly_refresh.ps1 -Unregister
param(
    [switch]$Register,
    [switch]$Unregister,
    [string]$Day = "Tuesday",       # after the weekend's fixtures are settled
    [string]$Time = "08:00"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = "FPL Squad Assistant - Weekly Refresh"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    return
}

if ($Register) {
    $pwshPath = (Get-Process -Id $PID).Path
    $action = New-ScheduledTaskAction -Execute $pwshPath `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Refresh FPL data, history and news weekly." -Force | Out-Null
    Write-Host "Registered '$TaskName' for every $Day at $Time." -ForegroundColor Green
    Write-Host "Remove it later with: .\scripts\weekly_refresh.ps1 -Unregister"
    return
}

Set-Location $Root

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("refresh-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== FPL weekly refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Tee-Object -FilePath $log

& $python -m fpl_assistant.ingest --all 2>&1 | Tee-Object -FilePath $log -Append
& $python -m fpl_assistant.check_sources 2>&1 | Tee-Object -FilePath $log -Append

Write-Host "`nDone. Log: $log" -ForegroundColor Green
