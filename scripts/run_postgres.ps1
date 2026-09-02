$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pgCtl = "D:\PostgreSQL\17\bin\pg_ctl.exe"
$data = "D:\PostgreSQL\17\data"
$port = "15432"

foreach ($requiredPath in @($pgCtl, $data)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required PostgreSQL path is missing: $requiredPath"
    }
}

$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "postgres-local.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

& $pgCtl status -D $data 2>$null
if ($LASTEXITCODE -eq 0) {
    return
}

& $pgCtl start -D $data -l $logPath `
    -o ('"-p {0}"' -f $port) -w -t 60
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL startup failed: $LASTEXITCODE"
}
