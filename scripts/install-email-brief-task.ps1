<#
.SYNOPSIS
    Register a Windows Scheduled Task that emails the monthly portfolio brief.

.DESCRIPTION
    Fires every Saturday at the given time. The job (jobs.email_brief) only
    actually sends on the FIRST Saturday of the month — the weekly trigger plus
    the job's own guard keeps the "first Saturday" rule in one place.

    Run from an elevated PowerShell prompt in the MAIN checkout (where .venv,
    .env, credentials.json, and token.json live).

.EXAMPLE
    .\scripts\install-email-brief-task.ps1
    .\scripts\install-email-brief-task.ps1 -At 08:30
#>
param(
    [string]$At = "09:00",
    [string]$TaskName = "Portfolio tracker monthly brief email"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $PSScriptRoot "run_email_brief.bat"
if (-not (Test-Path $bat)) { throw "Launcher not found: $bat" }

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Emails the monthly portfolio brief on the first Saturday of each month." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' — fires Saturdays at $At; the job sends only on the first Saturday."
Write-Host "To run whether you're logged on or not, open Task Scheduler -> the task -> Properties -> General -> 'Run whether user is logged on or not'."
