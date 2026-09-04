$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "use_d_runtime.ps1")
$runtimeDir = Join-Path $repoRoot "openab\.runtime"
$binDir = Join-Path $runtimeDir "bin"
$npmDir = Join-Path $runtimeDir "npm"
$downloadUrl = "https://github.com/openabdev/openab/releases/download/openab-0.10.0-beta.2/openab-0.10.0-beta.2-windows-x64.zip"
$expectedSha256 = "cdf32e2a860c188668626cd9c4f7195bb0a18f597ece6953522644b045e4852f"
$archive = Join-Path $runtimeDir "openab-windows-x64.zip"

New-Item -ItemType Directory -Force $runtimeDir,$binDir,$npmDir | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $binDir "openab.exe"))) {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archive
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    if ($actual -ne $expectedSha256) {
        throw "OpenAB archive SHA-256 mismatch: expected $expectedSha256, got $actual"
    }
    $extractDir = Join-Path $runtimeDir "extract"
    New-Item -ItemType Directory -Force $extractDir | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $extractDir -Force
    $binary = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter openab.exe | Select-Object -First 1
    if (-not $binary) { throw "Downloaded archive does not contain openab.exe" }
    Copy-Item -LiteralPath $binary.FullName -Destination (Join-Path $binDir "openab.exe") -Force
}

if (-not (Test-Path -LiteralPath (Join-Path $npmDir "node_modules\@agentclientprotocol\codex-acp\dist\index.js"))) {
    & $RadarNpmCmd install --prefix $npmDir --no-audit --no-fund --save-exact `
        "@agentclientprotocol/codex-acp@1.1.4" "@openai/codex@0.145.0"
    if ($LASTEXITCODE -ne 0) { throw "npm install failed: $LASTEXITCODE" }
}

& (Join-Path $binDir "openab.exe") --version
& $RadarNodeExe (Join-Path $npmDir "node_modules\@agentclientprotocol\codex-acp\dist\index.js") --version
