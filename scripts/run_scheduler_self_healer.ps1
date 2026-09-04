$ErrorActionPreference = "Continue"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "scheduler-self-healer.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
& $RadarPythonExe `
    -X utf8 "scripts\scheduler_self_healer.py" 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $LASTEXITCODE
