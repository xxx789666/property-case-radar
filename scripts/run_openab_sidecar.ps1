$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "openab\.runtime"
$logDir = Join-Path $repoRoot "logs"
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$pythonExe = "C:\Users\xx\AppData\Local\Programs\Python\Python313\python.exe"
$agentEntry = Join-Path $runtimeDir "npm\node_modules\@agentclientprotocol\codex-acp\dist\index.js"
$bridgeServer = Join-Path $repoRoot "openab\sidecar\bridge-server.mjs"
$dbSecret = Join-Path $repoRoot "openab\.local\radar_agent_database_url"
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
if (-not (Test-Path -LiteralPath $dbSecret)) { throw "Read-only database secret is missing: $dbSecret" }
if (-not $downloadRoot) { throw "AUCTION_CAPTURE_DOWNLOAD_DIR is not configured" }
Set-Location -LiteralPath $repoRoot

$env:ACP_SIDECAR_LISTEN_HOST = "127.0.0.1"
$env:ACP_SIDECAR_LISTEN_PORT = "18765"
$env:ACP_AGENT_COMMAND = $nodeExe
$env:ACP_AGENT_ARGS_JSON = ConvertTo-Json -Compress @($agentEntry)
$env:ACP_AGENT_CWD = Join-Path $repoRoot "openab\sidecar"
$env:RADAR_AGENT_DATABASE_URL_SECRET_FILE = $dbSecret
$env:RADAR_AGENT_QUERY_LOG = $queryLog
$env:RADAR_AUCTION_DOWNLOAD_DIR = $downloadRoot
$env:RADAR_PDF_UPLOAD_BROKER_URL = "http://127.0.0.1:18766"
$env:PYTHONPATH = $repoRoot
$env:PYTHONUTF8 = "1"
# Scheduled Tasks do not reliably inherit the interactive user's PATH.
# Prepend the exact validated interpreter directory so AGENTS.md's fixed
# `python -m tools.radar_agent_query` command resolves in every ACP session.
$env:PATH = "$(Split-Path -Parent $pythonExe);$env:PATH"

"$(Get-Date -Format o) sidecar wrapper starting" | Add-Content -LiteralPath $componentLog -Encoding utf8
# bridge-server deliberately writes structured operational events to
# stderr. Windows PowerShell 5 wraps redirected native stderr as
# ErrorRecord objects; keep those records in the log instead of treating a
# normal "listening" event as a terminating PowerShell error.
$ErrorActionPreference = "Continue"
& $nodeExe $bridgeServer 2>&1 |
    ForEach-Object { "$(Get-Date -Format o) $_" | Add-Content -LiteralPath $componentLog -Encoding utf8 }
exit $LASTEXITCODE
