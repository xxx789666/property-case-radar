$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$scriptPath = Join-Path $repoRoot "scripts\run_database_backup.ps1"

$action = New-ScheduledTaskAction -Execute $powerShell -Argument (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath
)
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Property Case Radar Database Backup" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null

Get-ScheduledTask -TaskName "Property Case Radar Database Backup" |
    Get-ScheduledTaskInfo |
    Select-Object LastRunTime, LastTaskResult, NextRunTime
