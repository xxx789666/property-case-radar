$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "use_d_runtime.ps1")

$pgCtl = Join-Path $RadarPostgresBin "pg_ctl.exe"
$logDirectory = Join-Path $RadarRepoRoot "logs"
$logPath = Join-Path $logDirectory "postgres-local.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

& $pgCtl status -D $RadarPostgresData 2>$null
if ($LASTEXITCODE -eq 0) {
    return
}

& $pgCtl start -D $RadarPostgresData -l $logPath `
    -o ('"-p {0}"' -f $RadarPostgresPort) -w -t 60
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL startup failed: $LASTEXITCODE"
}
