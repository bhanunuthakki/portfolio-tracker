@echo off
REM Double-click setup helper. -NoExit keeps success or error details visible.
powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0install-api-server-task.ps1"
