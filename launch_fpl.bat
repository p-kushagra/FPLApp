@echo off
REM ===========================================================================
REM  FPL Command Center - one-click launcher
REM
REM  Starts the background scheduler daemon and the Streamlit dashboard, then
REM  opens the browser. Safe to run twice: the daemon refuses to start a second
REM  instance against the same database, and Streamlit will report the port.
REM
REM  Usage:  double-click, or  launch_fpl.bat
REM  Stop:   stop_fpl.bat
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PORT=8501"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

REM --- locate the interpreter ------------------------------------------------
if exist "%PYTHON%" (
    echo [1/4] Using project virtual environment.
    call "%~dp0.venv\Scripts\activate.bat" >nul 2>&1
) else (
    echo [1/4] No .venv found - falling back to system Python.
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   ERROR: Python was not found on PATH and .venv does not exist.
        echo   Create the environment first:
        echo       python -m venv .venv
        echo       .venv\Scripts\pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

REM --- background scheduler daemon -------------------------------------------
echo [2/4] Starting background scheduler daemon...
"%PYTHON%" -m fpl_assistant.daemon --status >nul 2>&1
if errorlevel 1 (
    start "FPL Daemon" /min "%PYTHON%" -m fpl_assistant.daemon
    echo       daemon starting - log: data\daemon.log
) else (
    echo       daemon already running - leaving it alone.
)

REM --- Streamlit dashboard ----------------------------------------------------
echo [3/4] Starting dashboard on http://localhost:%PORT% ...
start "FPL Dashboard" /min "%PYTHON%" -m streamlit run Refresh_Config.py ^
    --server.headless=true ^
    --server.port=%PORT% ^
    --server.address=localhost ^
    --browser.gatherUsageStats=false

REM --- wait for the server, then open the browser ------------------------------
REM  The readiness poll lives in Python, not here. A batch version needs a
REM  powershell -Command with nested quotes inside a caret-continued line, and
REM  that quoting fails silently: the probe never succeeds, the launcher falls
REM  through to its timeout branch and sits on a `pause` forever, leaving the
REM  lingering console this launcher exists to avoid.
echo [4/4] Waiting for the server to accept connections...
"%PYTHON%" "%~dp0scripts\open_dashboard.py" --port %PORT%
if errorlevel 1 goto failed

echo.
echo  ==========================================================
echo   FPL Command Center is running
echo     Dashboard : http://localhost:%PORT%
echo     Daemon log: data\daemon.log
echo     To stop   : stop_fpl.bat
echo  ==========================================================
echo.
REM  stdin-safe delay: `timeout` aborts with "Input redirection is not
REM  supported" whenever stdin is redirected (piped launch, scheduled task).
ping -n 4 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo   The dashboard did not come up. Check the "FPL Dashboard" window,
echo   or run this to see the error directly:
echo       .venv\Scripts\python -m streamlit run Refresh_Config.py
echo.
ping -n 6 127.0.0.1 >nul
exit /b 1
