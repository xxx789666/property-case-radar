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
$controlLog = Join-Path $logDirectory "postgres-self-heal-ctl.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

& $pgCtl status -D $data 2>$null
if ($LASTEXITCODE -eq 0) {
    return
}

# Start via hidden cmd with file redirection so the server process cannot
# keep the outer captured stdout/stderr pipes open after pg_ctl exits.
if (Test-Path -LiteralPath $controlLog) {
    Remove-Item -LiteralPath $controlLog -Force
}

$startCommand = '"{0}" start -D "{1}" -l "{2}" -o "-p {3}" -w -t 60 1>"{4}" 2>&1' -f @(
    $pgCtl,
    $data,
    $logPath,
    $port,
    $controlLog
)
$process = Start-Process -FilePath $env:ComSpec `
    -ArgumentList @('/c', $startCommand) `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
$exitCode = $process.ExitCode
if ($exitCode -eq 0) {
    return
}

$controlDetail = ""
if (Test-Path -LiteralPath $controlLog) {
    $controlDetail = (
        Get-Content -LiteralPath $controlLog -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}
if (-not $controlDetail) {
    $controlDetail = "no control log output"
}
throw "PostgreSQL startup failed: $exitCode; $controlDetail"
