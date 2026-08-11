$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Register-RadarTask {
    param([string]$Name, [string]$ScriptPath)

    $action = New-ScheduledTaskAction -Execute $powerShell -Argument (
        '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $ScriptPath
    )
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $settings = New-ScheduledTaskSettingsSet `
        -Hidden `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
}

Register-RadarTask -Name "Property Case Radar OpenAB Sidecar" `
    -ScriptPath (Join-Path $repoRoot "scripts\run_openab_sidecar.ps1")
Register-RadarTask -Name "Property Case Radar OpenAB Gateway" `
    -ScriptPath (Join-Path $repoRoot "scripts\run_openab_gateway.ps1")

Start-ScheduledTask -TaskName "Property Case Radar OpenAB Sidecar"
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "Property Case Radar OpenAB Gateway"

& (Join-Path $repoRoot "scripts\register_discord_qa_allowlist_task.ps1")
