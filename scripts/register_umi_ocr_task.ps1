$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskName = "Property Case Radar Umi-OCR"
$executable = "D:\Umi-OCR_Paddle_v2.1.5\Umi-OCR.exe"
$environmentPath = Join-Path $repoRoot ".env"

if (Test-Path -LiteralPath $environmentPath) {
    $configuredPath = Get-Content -LiteralPath $environmentPath | Where-Object {
        $_ -match '^AUCTION_CAPTURE_OCR_EXECUTABLE='
    } | Select-Object -Last 1
    if ($configuredPath) {
        $executable = ($configuredPath -split '=', 2)[1].Trim().Trim('"').Trim("'")
    }
}
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Umi-OCR executable not found: $executable"
}

Push-Location $repoRoot
try {
    & $RadarPythonExe -X utf8 "scripts\umi_ocr_service.py" `
        --ensure-compatibility $executable
    if ($LASTEXITCODE -ne 0) { throw "Umi-OCR compatibility check failed" }
}
finally {
    Pop-Location
}

$action = New-ScheduledTaskAction -Execute $executable `
    -WorkingDirectory (Split-Path -Parent $executable)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$trigger.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser -LogonType Interactive -RunLevel Limited

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and $existingTask.State -eq "Running") {
    Stop-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 2
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
