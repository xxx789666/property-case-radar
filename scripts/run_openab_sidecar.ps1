$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$runtimeDir = Join-Path $repoRoot "openab\.runtime"
$logDir = Join-Path $repoRoot "logs"
$nodeExe = $RadarNodeExe
$pythonExe = $RadarPythonExe
$agentEntry = Join-Path $runtimeDir "npm\node_modules\@agentclientprotocol\codex-acp\dist\index.js"
$bridgeServer = Join-Path $repoRoot "openab\sidecar\bridge-server.mjs"
$subscriptionBroker = Join-Path $repoRoot "scripts\subscription_broker.py"
$subscriptionBrokerLog = Join-Path $logDir "subscription-broker.log"
$subscriptionBrokerErrorLog = Join-Path $logDir "subscription-broker.stderr.log"
$subscriptionBrokerToken = Join-Path $repoRoot "openab\.local\subscription_broker_token"
$dbSecretSource = Join-Path $repoRoot "openab\.local\radar_agent_database_url"
$dbSecret = Join-Path $runtimeDir "radar_agent_database_url_local"
$queryLog = Join-Path $logDir "openab-query.jsonl"
$componentLog = Join-Path $logDir "openab-sidecar.log"
$downloadRoot = $env:AUCTION_CAPTURE_DOWNLOAD_DIR
if (-not $downloadRoot) {
    $envFile = Join-Path $repoRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $downloadSetting = Get-Content -LiteralPath $envFile -Encoding utf8 |
            Where-Object { $_ -match "^AUCTION_CAPTURE_DOWNLOAD_DIR=" } |
            Select-Object -First 1
        if ($downloadSetting) {
            $downloadRoot = $downloadSetting.Substring($downloadSetting.IndexOf("=") + 1).Trim().Trim('"').Trim("'")
        }
    }
}

New-Item -ItemType Directory -Force $logDir | Out-Null
if (-not (Test-Path -LiteralPath $agentEntry)) { throw "codex-acp is not installed: $agentEntry" }
if (-not (Test-Path -LiteralPath $pythonExe)) { throw "Radar Python is not installed: $pythonExe" }
if (-not (Test-Path -LiteralPath $dbSecretSource)) { throw "Read-only database secret is missing: $dbSecretSource" }
if (-not (Test-Path -LiteralPath $subscriptionBroker)) { throw "Subscription broker is missing: $subscriptionBroker" }
if (-not $downloadRoot) { throw "AUCTION_CAPTURE_DOWNLOAD_DIR is not configured" }
Set-Location -LiteralPath $repoRoot

$localDatabaseUrl = [IO.File]::ReadAllText($dbSecretSource).Trim().Replace(
    "@127.0.0.1:5432/",
    "@127.0.0.1:$RadarPostgresPort/"
).Replace(
    "@localhost:5432/",
    "@localhost:$RadarPostgresPort/"
)
[IO.File]::WriteAllText($dbSecret, $localDatabaseUrl, [Text.UTF8Encoding]::new($false))

# Task Scheduler can terminate this PowerShell host before its finally
# block runs, leaving an older subscription broker alive on port 18767.
# Remove only orphaned Python processes whose command line contains this
# exact project script before starting the single current broker.
$subscriptionBrokerFull = [IO.Path]::GetFullPath($subscriptionBroker)
$staleBrokers = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.IndexOf(
                $subscriptionBrokerFull,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        }
)
foreach ($staleBroker in $staleBrokers) {
    Stop-Process -Id $staleBroker.ProcessId -Force -ErrorAction Stop
}
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $listener = Get-NetTCPConnection -LocalPort 18767 -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { break }
    Start-Sleep -Milliseconds 250
}
if (Get-NetTCPConnection -LocalPort 18767 -State Listen -ErrorAction SilentlyContinue) {
    throw "Previous subscription broker did not release port 18767"
}

$env:ACP_SIDECAR_LISTEN_HOST = "127.0.0.1"
$env:ACP_SIDECAR_LISTEN_PORT = "18765"
$env:ACP_AGENT_COMMAND = $nodeExe
$env:ACP_AGENT_ARGS_JSON = ConvertTo-Json -Compress @($agentEntry)
$env:ACP_AGENT_CWD = Join-Path $repoRoot "openab\sidecar"
$env:RADAR_AGENT_DATABASE_URL_SECRET_FILE = $dbSecret
$env:RADAR_AGENT_QUERY_LOG = $queryLog
$env:RADAR_AUCTION_DOWNLOAD_DIR = $downloadRoot
$env:RADAR_PDF_UPLOAD_BROKER_URL = "http://127.0.0.1:18766"
$env:RADAR_SUBSCRIPTION_BROKER_URL = "http://127.0.0.1:18767"
$env:RADAR_SUBSCRIPTION_BROKER_TOKEN_FILE = $subscriptionBrokerToken
$env:PYTHONPATH = $repoRoot
$env:PYTHONUTF8 = "1"
# Scheduled Tasks do not reliably inherit the interactive user's PATH.
# Prepend the exact validated interpreter directory so AGENTS.md's fixed
# `python -m tools.radar_agent_query` command resolves in every ACP session.
$env:PATH = "$(Split-Path -Parent $pythonExe);$env:PATH"

"$(Get-Date -Format o) sidecar wrapper starting" | Add-Content -LiteralPath $componentLog -Encoding utf8
$subscriptionBrokerProcess = Start-Process -FilePath $pythonExe `
    -ArgumentList @("-X", "utf8", $subscriptionBroker) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $subscriptionBrokerLog `
    -RedirectStandardError $subscriptionBrokerErrorLog `
    -PassThru
$brokerReady = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if ($subscriptionBrokerProcess.HasExited) {
        throw "Subscription broker exited during startup with code $($subscriptionBrokerProcess.ExitCode)"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:18767/health" -TimeoutSec 1
        if ($health.ok -eq $true) {
            $brokerReady = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
if (-not $brokerReady) { throw "Subscription broker did not become ready on port 18767" }
# bridge-server deliberately writes structured operational events to
# stderr. Windows PowerShell 5 wraps redirected native stderr as
# ErrorRecord objects; keep those records in the log instead of treating a
# normal "listening" event as a terminating PowerShell error.
$ErrorActionPreference = "Continue"
try {
    & $nodeExe $bridgeServer 2>&1 |
        ForEach-Object { "$(Get-Date -Format o) $_" | Add-Content -LiteralPath $componentLog -Encoding utf8 }
    $bridgeExitCode = $LASTEXITCODE
} finally {
    if ($null -ne $subscriptionBrokerProcess -and -not $subscriptionBrokerProcess.HasExited) {
        Stop-Process -Id $subscriptionBrokerProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
exit $bridgeExitCode
