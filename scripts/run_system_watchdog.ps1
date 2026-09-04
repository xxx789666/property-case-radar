$ErrorActionPreference = "Continue"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "system-watchdog.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$env:PYTHONUTF8 = "1"
& $RadarPythonExe `
    "scripts\system_watchdog.py" 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $LASTEXITCODE
