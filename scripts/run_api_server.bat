@echo off
REM API-server launcher for Windows Task Scheduler (at-logon trigger).
REM Serves the FastAPI on 127.0.0.1:8000 — earnings-summary's morning
REM pipeline (Stage 0c weights + Stage 0f fit) does a LIVE fetch against it,
REM so this process must survive reboots; ad-hoc detached uvicorns died on
REM every restart (2026-07-16: two fit-cache regressions in one day).
REM Resolves the venv Python so we don't depend on PATH. Appends to a daily
REM log under scripts\logs for debugging silent failures.

setlocal
set ROOT=%~dp0..
set PYTHON=%ROOT%\.venv\Scripts\python.exe
set LOG_DIR=%ROOT%\scripts\logs

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

cd /d "%ROOT%"
REM -u: unbuffered so the log captures startup errors as they happen.
"%PYTHON%" -u -m uvicorn portfolio_tracker.api.main:app --host 127.0.0.1 --port 8000 >> "%LOG_DIR%\api_server_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
