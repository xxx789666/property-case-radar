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
$stdoutLog = Join-Path $logDirectory "postgres-self-heal-ctl.out.log"
$stderrLog = Join-Path $logDirectory "postgres-self-heal-ctl.err.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

& $pgCtl status -D $data 2>$null
if ($LASTEXITCODE -eq 0) {
    return
}

foreach ($controlFile in @($stdoutLog, $stderrLog)) {
    if (Test-Path -LiteralPath $controlFile) {
        Remove-Item -LiteralPath $controlFile -Force
    }
}

$argumentList = @(
    "start",
    "-D", ('"{0}"' -f ($data -replace '"', '\"')),
    "-l", ('"{0}"' -f ($logPath -replace '"', '\"')),
    "-o", ('"-p {0}"' -f $port),
    "-w",
    "-t", "60"
)
$process = Start-Process -FilePath $pgCtl `
    -ArgumentList $argumentList `
    -PassThru `
    -NoNewWindow `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog
$null = $process.Handle
if (-not $process.WaitForExit(70000)) {
    if (-not $process.HasExited) {
        $process.Kill()
    }
    throw "PostgreSQL startup timed out waiting for pg_ctl"
}
$exitCode = $process.ExitCode
if ($exitCode -eq 0) {
    return
}

$stderrDetail = ""
if (Test-Path -LiteralPath $stderrLog) {
    $stderrDetail = (
        Get-Content -LiteralPath $stderrLog -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}
$stdoutDetail = ""
if (Test-Path -LiteralPath $stdoutLog) {
    $stdoutDetail = (
        Get-Content -LiteralPath $stdoutLog -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}
$controlDetail = $stderrDetail
if ($stdoutDetail) {
    if ($controlDetail) {
        $controlDetail = $controlDetail + [Environment]::NewLine + $stdoutDetail
    } else {
        $controlDetail = $stdoutDetail
    }
}
if (-not $controlDetail) {
    $controlDetail = "no control log output"
}
throw "PostgreSQL startup failed: $exitCode; $controlDetail"
