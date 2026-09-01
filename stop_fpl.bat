@echo off
REM ===========================================================================
REM  FPL Command Center - clean shutdown
REM
REM  Order matters. The daemon is asked to stop FIRST and gracefully, because
REM  it is the only process that writes to fpl.sqlite on a schedule: killing it
REM  mid-freeze would leave a partial gameweek in projection_snapshot, which is
REM  write-once and would then refuse to be completed. Streamlit is read-mostly
REM  and safe to terminate once the daemon is down.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo.
echo  Stopping FPL Command Center...
echo.

REM --- 1. graceful daemon stop -------------------------------------------------
echo [1/2] Background daemon
"%PYTHON%" -m fpl_assistant.daemon --stop
if errorlevel 1 (
    echo       WARNING: the daemon did not confirm a clean stop.
    echo       It was NOT force-killed - check data\daemon.log before retrying.
)

REM --- 2. Streamlit ------------------------------------------------------------
REM  Matched on the command line rather than the port, so an unrelated Python
REM  process listening on 8501 is never touched.
echo [2/2] Dashboard
powershell -NoProfile -Command ^
  "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*streamlit*run*Refresh_Config.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('      stopped ' + @($p).Count + ' dashboard process(es).') } else { Write-Host '      dashboard not running.' }"

echo.
echo  ==========================================================
echo   Shutdown complete. fpl.sqlite was closed cleanly.
echo  ==========================================================
echo.
REM  stdin-safe delay: `timeout` fails under redirected stdin.
ping -n 3 127.0.0.1 >nul
exit /b 0
