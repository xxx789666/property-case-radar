$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$runtimeDir = Join-Path $repoRoot "openab\.runtime"
$localDir = Join-Path $repoRoot "openab\.local"
$logDir = Join-Path $repoRoot "logs"
$tokenSecret = Join-Path $localDir "openab_discord_bot_token"
$roleIdFile = Join-Path $localDir "discord_qa_role_id"
$verificationChannelIdFile = Join-Path $localDir "discord_verification_channel_id"
$verificationMessageIdFile = Join-Path $localDir "discord_verification_message_id"
$allowlistFile = Join-Path $runtimeDir "discord-qa-allowed-users.json"
$syncScript = Join-Path $repoRoot "scripts\sync_discord_qa_allowlist.py"
$logFile = Join-Path $logDir "discord-qa-allowlist-sync.log"
$gatewayTask = "Property Case Radar OpenAB Gateway"

New-Item -ItemType Directory -Force $runtimeDir,$logDir | Out-Null
$output = & $RadarPythonExe -X utf8 $syncScript `
    --token-file $tokenSecret `
    --role-id-file $roleIdFile `
    --allowlist-file $allowlistFile `
    --verification-channel-id-file $verificationChannelIdFile `
    --verification-message-id-file $verificationMessageIdFile `
    --changed-exit-code 10 2>&1
$syncExitCode = $LASTEXITCODE
"$(Get-Date -Format o) $output" | Add-Content -LiteralPath $logFile -Encoding utf8

if ($syncExitCode -eq 10) {
    Stop-ScheduledTask -TaskName $gatewayTask -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(20)
    do {
        $listener = Get-NetTCPConnection -LocalPort 18766 -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($listener) { throw "PDF broker port 18766 did not release during allowlist reload" }
    Start-ScheduledTask -TaskName $gatewayTask -ErrorAction Stop
} elseif ($syncExitCode -ne 0) {
    throw "Discord Q&A allowlist sync failed with exit code $syncExitCode"
}
