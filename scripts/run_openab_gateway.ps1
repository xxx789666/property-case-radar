$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "openab\.runtime"
$logDir = Join-Path $repoRoot "logs"
$openabExe = Join-Path $runtimeDir "bin\openab.exe"
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$configTemplate = Join-Path $repoRoot "openab\windows\config-radar-agent.toml"
$runtimeConfig = Join-Path $runtimeDir "config-radar-agent.toml"
$tokenSecret = Join-Path $repoRoot "openab\.local\openab_discord_bot_token"
$componentLog = Join-Path $logDir "openab-gateway.log"
$brokerScript = Join-Path $repoRoot "openab\gateway\pdf-upload-broker.mjs"
$brokerLog = Join-Path $logDir "openab-pdf-upload-broker.jsonl"
$brokerStderr = Join-Path $logDir "openab-pdf-upload-broker.stderr.log"
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

New-Item -ItemType Directory -Force $runtimeDir,$logDir | Out-Null
if (-not (Test-Path -LiteralPath $openabExe)) { throw "OpenAB is not installed: $openabExe" }
if (-not (Test-Path -LiteralPath $tokenSecret)) { throw "OpenAB Discord token secret is missing: $tokenSecret" }
if (-not (Test-Path -LiteralPath $brokerScript)) { throw "PDF upload broker is missing: $brokerScript" }
if (-not $downloadRoot) { throw "AUCTION_CAPTURE_DOWNLOAD_DIR is not configured" }
if (-not (Test-Path -LiteralPath $downloadRoot -PathType Container)) {
    throw "Auction download directory is missing: $downloadRoot"
}
Set-Location -LiteralPath $repoRoot

$brokerProcess = $null
$env:OPENAB_DISCORD_BOT_TOKEN = [IO.File]::ReadAllText($tokenSecret).Trim()
try {
    & python -X utf8 (Join-Path $repoRoot "openab\gateway\runtime_config.py") $configTemplate $runtimeConfig
    if ($LASTEXITCODE -ne 0) { throw "OpenAB runtime config rendering failed: $LASTEXITCODE" }

    $env:RADAR_AUCTION_DOWNLOAD_DIR = $downloadRoot
    $env:RADAR_PDF_ALLOWED_PARENT_IDS = "1530076529751756870"
    $env:RADAR_PDF_BROKER_LOG = $brokerLog
    $env:RADAR_PDF_BROKER_PARENT_PID = [string]$PID
    $env:RADAR_DISCORD_API_BASE_URL = "https://discord.com/api/v10"
    $brokerProcess = Start-Process -FilePath $nodeExe `
        -ArgumentList @($brokerScript) `
        -WindowStyle Hidden `
        -RedirectStandardError $brokerStderr `
        -PassThru

    $brokerReady = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($brokerProcess.HasExited) {
            throw "PDF upload broker exited during startup with code $($brokerProcess.ExitCode)"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:18766/health" -TimeoutSec 1
            if ($health.ok -eq $true) {
                $brokerReady = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $brokerReady) { throw "PDF upload broker did not become ready on port 18766" }
} finally {
    Remove-Item Env:OPENAB_DISCORD_BOT_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:RADAR_PDF_BROKER_PARENT_PID -ErrorAction SilentlyContinue
    Remove-Item Env:RADAR_DISCORD_API_BASE_URL -ErrorAction SilentlyContinue
}

$env:RADAR_BRIDGE_CLIENT = (Join-Path $repoRoot "openab\gateway\bridge-client.mjs") -replace "\\","/"
$env:RADAR_AGENT_WORKING_DIR = (Join-Path $repoRoot "openab\sidecar") -replace "\\","/"
$env:HOME = $env:USERPROFILE
$env:RUST_BACKTRACE = "0"

"$(Get-Date -Format o) gateway wrapper starting" | Add-Content -LiteralPath $componentLog -Encoding utf8
try {
    & $openabExe run -c $runtimeConfig 2>&1 |
        ForEach-Object { "$(Get-Date -Format o) $_" | Add-Content -LiteralPath $componentLog -Encoding utf8 }
    $gatewayExitCode = $LASTEXITCODE
} finally {
    if ($null -ne $brokerProcess -and -not $brokerProcess.HasExited) {
        Stop-Process -Id $brokerProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
exit $gatewayExitCode
