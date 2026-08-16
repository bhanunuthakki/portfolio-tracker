<#
.SYNOPSIS
    Register a Windows Scheduled Task that keeps the portfolio-tracker API
    server (uvicorn, 127.0.0.1:8000) running from system startup.

.DESCRIPTION
    earnings-summary's morning pipeline (Stage 0c weights + Stage 0f candidate
    fit) does a LIVE fetch against this API. Before this task existed the
    server was started ad hoc and died on every reboot/logoff, silently
    degrading all 37 evaluation fits ("tracker offline") — twice on
    2026-07-16 alone.

    Trigger: at startup. The task runs as the supplied Windows account with a
    password-backed noninteractive logon, so it can restore the loopback API
    after a reboot before anyone signs in. Restart-on-failure: 3 attempts,
    1 minute apart, and no execution time limit (it's a server, not a job).
    uvicorn binds 127.0.0.1; a second instance exits immediately on the port
    conflict, so an already-running server is harmless.

    Run from a PowerShell prompt in the tracker checkout (where .venv lives).

.EXAMPLE
    .\scripts\install-api-server-task.ps1
    .\scripts\install-api-server-task.ps1 -StartNow
    .\scripts\install-api-server-task.ps1 -AtLogOn
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PortfolioTrackerApiServer",
    [switch]$StartNow,
    [switch]$AtLogOn,
    [PSCredential]$RunAsCredential
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $PSScriptRoot "run_api_server.bat"
if (-not (Test-Path $bat)) { throw "Launcher not found: $bat" }

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

if ($AtLogOn) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Keeps the portfolio-tracker FastAPI (127.0.0.1:8000) up from logon." `
        -Force | Out-Null
    Write-Warning "Registered '$TaskName' for interactive logon only; it will not restart unattended after reboot."
} else {
    if ($null -eq $RunAsCredential) {
        $RunAsCredential = Get-Credential -Message "Enter the Windows account that should run $TaskName after reboot."
    }
    if ([string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
        throw "A Windows account is required to run '$TaskName' after reboot."
    }

    $trigger = New-ScheduledTaskTrigger -AtStartup
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $RunAsCredential.UserName `
        -Password $RunAsCredential.GetNetworkCredential().Password `
        -RunLevel Limited `
        -Description "Keeps the portfolio-tracker FastAPI (127.0.0.1:8000) up after reboot. earnings-summary reads it live." `
        -Force | Out-Null
    Write-Host "Registered '$TaskName' to start the loopback API after reboot without interactive logon."
}

if ($StartNow) {
    $task = Get-ScheduledTask -TaskName $TaskName
    if ($task.State -eq "Running") {
        Write-Host "Task definition updated; its existing API process remains running until the next restart."
    } else {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Started '$TaskName' now."
    }
}
