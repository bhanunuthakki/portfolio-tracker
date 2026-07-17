<#
.SYNOPSIS
    Register a Windows Scheduled Task that keeps the portfolio-tracker API
    server (uvicorn, 127.0.0.1:8000) running from logon.

.DESCRIPTION
    earnings-summary's morning pipeline (Stage 0c weights + Stage 0f candidate
    fit) does a LIVE fetch against this API. Before this task existed the
    server was started ad hoc and died on every reboot/logoff, silently
    degrading all 37 evaluation fits ("tracker offline") — twice on
    2026-07-16 alone.

    Trigger: at logon. Restart-on-failure: 3 attempts, 1 minute apart, and
    no execution time limit (it's a server, not a job). uvicorn binds
    127.0.0.1:8000; a second instance exits immediately on the port conflict,
    so an already-running ad-hoc server is harmless.

    Run from a PowerShell prompt in the tracker checkout (where .venv lives).

.EXAMPLE
    .\scripts\install-api-server-task.ps1
    .\scripts\install-api-server-task.ps1 -StartNow
#>
param(
    [string]$TaskName = "PortfolioTrackerApiServer",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $PSScriptRoot "run_api_server.bat"
if (-not (Test-Path $bat)) { throw "Launcher not found: $bat" }

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Keeps the portfolio-tracker FastAPI (127.0.0.1:8000) up from logon. earnings-summary Stage 0c/0f fit legs read it live." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' - starts the API server at logon, restarts up to 3x on failure."
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started '$TaskName' now."
}
