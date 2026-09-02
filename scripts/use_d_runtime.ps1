$ErrorActionPreference = "Stop"

$RadarRepoRoot = Split-Path -Parent $PSScriptRoot
$RadarRuntimeRoot = Join-Path $RadarRepoRoot ".runtime"
$RadarPythonExe = Join-Path $RadarRuntimeRoot "python312\python.exe"
$RadarNodeExe = Join-Path $RadarRuntimeRoot "nodejs\node.exe"
$RadarNpmCmd = Join-Path $RadarRuntimeRoot "nodejs\npm.cmd"
$RadarPostgresBin = "D:\PostgreSQL\17\bin"
$RadarPostgresData = "D:\PostgreSQL\17\data"
$RadarPostgresPort = "15432"

foreach ($requiredPath in @(
    $RadarPythonExe,
    $RadarNodeExe,
    (Join-Path $RadarPostgresBin "pg_ctl.exe"),
    $RadarPostgresData
)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required D-drive runtime path is missing: $requiredPath"
    }
}

$runtimeHome = Join-Path $RadarRuntimeRoot "home"
$runtimeRoaming = Join-Path $runtimeHome "AppData\Roaming"
$runtimeLocal = Join-Path $runtimeHome "AppData\Local"
$runtimeTemp = Join-Path $RadarRuntimeRoot "temp"
$runtimeCache = Join-Path $RadarRuntimeRoot "cache"
$runtimePycache = Join-Path $RadarRuntimeRoot "pycache"
$runtimePlaywright = Join-Path $RadarRuntimeRoot "playwright"

New-Item -ItemType Directory -Force -Path @(
    $runtimeHome,
    $runtimeRoaming,
    $runtimeLocal,
    $runtimeTemp,
    $runtimeCache,
    $runtimePycache,
    $runtimePlaywright
) | Out-Null

$env:HOME = $runtimeHome
$env:USERPROFILE = $runtimeHome
$env:APPDATA = $runtimeRoaming
$env:LOCALAPPDATA = $runtimeLocal
$env:TEMP = $runtimeTemp
$env:TMP = $runtimeTemp
$env:PIP_CACHE_DIR = Join-Path $runtimeCache "pip"
$env:XDG_CACHE_HOME = Join-Path $runtimeCache "xdg"
$env:PYTHONPYCACHEPREFIX = $runtimePycache
$env:PLAYWRIGHT_BROWSERS_PATH = $runtimePlaywright
$env:PYTHONPATH = $RadarRepoRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PGHOST = "127.0.0.1"
$env:PGPORT = $RadarPostgresPort

$envFile = Join-Path $RadarRepoRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    $databaseSetting = Get-Content -LiteralPath $envFile -Encoding utf8 |
        Where-Object { $_ -match "^DATABASE_URL=" } |
        Select-Object -Last 1
    if ($databaseSetting) {
        $databaseUrl = $databaseSetting.Substring($databaseSetting.IndexOf("=") + 1).Trim().Trim('"').Trim("'")
        $databaseUrl = $databaseUrl.Replace(
            "@127.0.0.1:5432/",
            "@127.0.0.1:$RadarPostgresPort/"
        ).Replace(
            "@localhost:5432/",
            "@localhost:$RadarPostgresPort/"
        )
        $env:DATABASE_URL = $databaseUrl
    }
}

$env:PATH = @(
    (Join-Path $RadarRuntimeRoot "python312"),
    (Join-Path $RadarRuntimeRoot "python312\Scripts"),
    (Join-Path $RadarRuntimeRoot "nodejs"),
    $RadarPostgresBin,
    $env:PATH
) -join ";"
