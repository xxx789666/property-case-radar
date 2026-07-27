$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$botFile = "D:\bot.txt"
$secretFile = Join-Path $repoRoot "openab\.local\openab_discord_bot_token"
$envFile = Join-Path $repoRoot ".env"
$expectedBotId = "1530136356439719997"

if (-not (Test-Path -LiteralPath $botFile)) { throw "D:\bot.txt does not exist" }
if (-not (Test-Path -LiteralPath $secretFile)) { throw "Local OpenAB token secret does not exist" }

$tokenPattern = "^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}$"
$tokenCandidates = @(
    [IO.File]::ReadAllLines($botFile) |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match $tokenPattern }
)
$oldToken = [IO.File]::ReadAllText($secretFile).Trim()
$validCandidates = @()
foreach ($candidate in $tokenCandidates) {
    try {
        $identity = Invoke-RestMethod `
            -Uri "https://discord.com/api/v10/users/@me" `
            -Headers @{ Authorization = "Bot $candidate" } `
            -TimeoutSec 10
        if ([string]$identity.id -eq $expectedBotId) {
            $validCandidates += [pscustomobject]@{ Token = $candidate; Identity = $identity }
        }
    } catch {
        # Invalid/revoked candidates are deliberately ignored.
    }
}
if ($validCandidates.Count -ne 1) {
    throw "Expected exactly one new valid token for Property Case Radar; found $($validCandidates.Count)"
}
$newToken = $validCandidates[0].Token
$newIdentity = $validCandidates[0].Identity

$oldTokenRejected = $false
try {
    Invoke-RestMethod `
        -Uri "https://discord.com/api/v10/users/@me" `
        -Headers @{ Authorization = "Bot $oldToken" } `
        -TimeoutSec 10 |
        Out-Null
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 401) {
        $oldTokenRejected = $true
    }
}

$tempSecret = Join-Path `
    (Split-Path -Parent $secretFile) `
    ("token-update-" + [guid]::NewGuid().ToString("N") + ".tmp")
try {
    [IO.File]::WriteAllText($tempSecret, $newToken, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempSecret -Destination $secretFile -Force

    if (-not (Test-Path -LiteralPath $envFile)) {
        throw "Project .env file does not exist"
    }
    $envLines = [IO.File]::ReadAllLines($envFile)
    $discordTokenLineFound = $false
    for ($index = 0; $index -lt $envLines.Count; $index++) {
        if ($envLines[$index] -match "^DISCORD_TOKEN=") {
            $envLines[$index] = "DISCORD_TOKEN=$newToken"
            $discordTokenLineFound = $true
        }
    }
    if (-not $discordTokenLineFound) {
        $envLines += "DISCORD_TOKEN=$newToken"
    }
    $tempEnv = Join-Path `
        (Split-Path -Parent $envFile) `
        (".env-token-update-" + [guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::WriteAllLines($tempEnv, $envLines, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempEnv -Destination $envFile -Force
} finally {
    if (Test-Path -LiteralPath $tempSecret) {
        Remove-Item -LiteralPath $tempSecret -Force
    }
    if ($tempEnv -and (Test-Path -LiteralPath $tempEnv)) {
        Remove-Item -LiteralPath $tempEnv -Force
    }
    Remove-Variable newToken,oldToken,tokenCandidates,validCandidates,envLines -ErrorAction SilentlyContinue
}

[pscustomobject]@{
    NewTokenValid = $true
    BotId = [string]$newIdentity.id
    BotName = [string]$newIdentity.username
    PreviousTokenRejected = $oldTokenRejected
    LocalSecretUpdated = $true
    SchedulerTokenUpdated = $true
}
