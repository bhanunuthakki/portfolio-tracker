@echo off
REM Daily-refresh launcher for Windows Task Scheduler.
REM Resolves to the project's venv Python so we don't depend on PATH.
REM Logs each run into scripts\logs\ — useful for debugging silent failures.

setlocal
set ROOT=%~dp0..
set PYTHON=%ROOT%\.venv\Scripts\python.exe
set LOG_DIR=%ROOT%\scripts\logs

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ISO date stamp for the log file (YYYY-MM-DD).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

cd /d "%ROOT%"
REM `-u` makes stdout/stderr unbuffered so the log captures progress as it
REM happens, not just at exit. Crucial for diagnosing a scheduled-task
REM crash where you'd otherwise see an empty log.
"%PYTHON%" -u -m portfolio_tracker.jobs.daily_refresh >> "%LOG_DIR%\daily_refresh_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
