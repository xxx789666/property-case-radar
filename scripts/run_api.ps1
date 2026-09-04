$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
& (Join-Path $PSScriptRoot "run_postgres.ps1")

$logDirectory = Join-Path $RadarRepoRoot "logs"
$logPath = Join-Path $logDirectory "api.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $RadarRepoRoot

# Windows PowerShell 5 wraps normal native stderr (including uvicorn INFO
# messages) as ErrorRecord objects. Preserve them in the log without treating
# successful server startup as a terminating PowerShell error.
$ErrorActionPreference = "Continue"
& $RadarPythonExe -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $LASTEXITCODE
