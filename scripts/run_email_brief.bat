@echo off
REM Monthly-brief email launcher for Windows Task Scheduler.
REM
REM Schedule this WEEKLY on Saturdays (see install-email-brief-task.ps1). The
REM job self-gates and only sends on the FIRST Saturday of the month, so a
REM plain weekly trigger is all you need.
REM
REM ANTHROPIC_API_KEY is unset so that if the brief needs generating, the
REM Claude call routes through the Pro/Max subscription via claude_cli, not
REM the metered API. PYTHONIOENCODING=utf-8 keeps the log safe on Windows
REM when the brief HTML carries typographic characters.

setlocal
set ROOT=%~dp0..
set PYTHON=%ROOT%\.venv\Scripts\python.exe
set LOG_DIR=%ROOT%\scripts\logs
set ANTHROPIC_API_KEY=
set PYTHONIOENCODING=utf-8

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ISO date stamp for the log file (YYYY-MM-DD).
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

cd /d "%ROOT%"
"%PYTHON%" -u -m portfolio_tracker.jobs.email_brief >> "%LOG_DIR%\email_brief_%TODAY%.log" 2>&1
exit /b %ERRORLEVEL%
