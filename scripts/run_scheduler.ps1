$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "scheduler.log"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] starting property-case-radar scheduler" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
& "C:\Users\xx\AppData\Local\Programs\Python\Python313\python.exe" `
    -m apps.scheduler.main 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8

$exitCode = $LASTEXITCODE
$timestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$timestamp] scheduler exited with code $exitCode" |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $exitCode
