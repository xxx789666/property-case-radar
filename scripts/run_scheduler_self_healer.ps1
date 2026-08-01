$ErrorActionPreference = "Continue"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $repositoryRoot "logs"
$logPath = Join-Path $logDirectory "scheduler-self-healer.log"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Set-Location -LiteralPath $repositoryRoot

$env:PYTHONUTF8 = "1"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
& "C:\Users\xx\AppData\Local\Programs\Python\Python313\python.exe" `
    -X utf8 "scripts\scheduler_self_healer.py" 2>&1 |
    Out-File -LiteralPath $logPath -Append -Encoding utf8
exit $LASTEXITCODE
