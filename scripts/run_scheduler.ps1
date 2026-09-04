$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
& (Join-Path $PSScriptRoot "run_postgres.ps1")
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "scheduler.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] starting property-case-radar scheduler" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
& $RadarPythonExe `
    -m apps.scheduler.main 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$exitCode = $LASTEXITCODE
$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] scheduler exited with code $exitCode" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $exitCode
