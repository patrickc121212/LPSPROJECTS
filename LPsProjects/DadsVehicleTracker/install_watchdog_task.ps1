<#
  Registers the "Dads Vehicle Tracker Watchdog" scheduled task.

  Why this exists as a separate task: the health monitor runs inside app.py,
  so it cannot report the app being down. On 2026-10-14 the app and its
  supervisor died together and the tracker was dead for five days with no
  alert. This task checks from the outside, every five minutes.

  Run it in a normal (non-admin) PowerShell window:
      powershell -ExecutionPolicy Bypass -File .\install_watchdog_task.ps1

  It runs as you, in your session, so no UAC prompt and no stored password.
  The trade-off: it only runs while you are logged on. The app's own task is
  S4U and still starts at boot, so a reboot you do not log into is covered
  by that task, not this one.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = "Dads Vehicle Tracker Watchdog"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "watchdog.py" -WorkingDirectory $root

# Check every 5 minutes, forever, from logon onward.
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$atLogon.Repetition = $repeating.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action `
    -Trigger @($atLogon, $repeating) -Settings $settings -Principal $principal `
    -Description "Checks every 5 min that the tracker serves on port 5000; restarts it and pushes an alert if not." `
    -Force | Out-Null

Write-Host "Registered '$name'." -ForegroundColor Green
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 8
Get-ScheduledTaskInfo -TaskName $name |
    Format-List TaskName, LastRunTime, LastTaskResult, NextRunTime
Write-Host "LastTaskResult 0 = the app was up. Log: data\watchdog.log"
