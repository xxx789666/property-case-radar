$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "database-backup.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] starting PostgreSQL backup and restore verification" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
& $RadarPythonExe `
    "scripts\backup_postgres.py" 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$exitCode = $LASTEXITCODE
$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] database backup exited with code $exitCode" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $exitCode
