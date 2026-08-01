[CmdletBinding()]
param(
    [string]$ServiceName = "postgresql-x64-17",
    [string]$SourceData = "C:\Program Files\PostgreSQL\17\data",
    [string]$TargetData = "D:\PostgreSQL\17\data"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $repoRoot "logs\postgres-migration.log"
$taskNames = @(
    "Property Case Radar Scheduler",
    "Property Case Radar OpenAB Gateway",
    "Property Case Radar OpenAB Sidecar"
)
$pgCtl = "C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe"
$oldBinPath = "`"$pgCtl`" runservice -N `"$ServiceName`" -D `"$SourceData`" -w"
$newBinPath = "`"$pgCtl`" runservice -N `"$ServiceName`" -D `"$TargetData`" -w"
$serviceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"

function Write-MigrationLog([string]$Message) {
    "$(Get-Date -Format o) $Message" |
        Add-Content -LiteralPath $logPath -Encoding utf8
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "This migration must run from an elevated PowerShell window."
}

$sourceFull = [IO.Path]::GetFullPath($SourceData)
$targetFull = [IO.Path]::GetFullPath($TargetData)
if (
    $sourceFull -ne "C:\Program Files\PostgreSQL\17\data" -or
    $targetFull -ne "D:\PostgreSQL\17\data" -or
    $sourceFull -eq $targetFull
) {
    throw "Refusing unapproved PostgreSQL source or target path."
}
if (-not (Test-Path -LiteralPath (Join-Path $sourceFull "PG_VERSION"))) {
    throw "Source does not look like a PostgreSQL data directory: $sourceFull"
}

New-Item -ItemType Directory -Force (Split-Path -Parent $logPath) | Out-Null
Write-MigrationLog "migration starting source=$sourceFull target=$targetFull"

$serviceChanged = $false
try {
    foreach ($taskName in $taskNames) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus("Stopped", "00:00:30")
    Write-MigrationLog "Radar tasks and PostgreSQL stopped"

    New-Item -ItemType Directory -Force $targetFull | Out-Null
    & robocopy $sourceFull $targetFull /MIR /COPYALL /DCOPY:DAT /R:2 /W:1 /XJ /NFL /NDL /NP
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $targetFull "PG_VERSION"))) {
        throw "Copied target is missing PG_VERSION"
    }
    & icacls $targetFull /grant "*S-1-5-20:(OI)(CI)F" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to grant NetworkService access to target"
    }

    Set-ItemProperty -LiteralPath $serviceRegistryPath -Name ImagePath -Value $newBinPath
    $serviceChanged = $true
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus("Running", "00:01:00")
    Write-MigrationLog "PostgreSQL started from D drive"
} catch {
    Write-MigrationLog "migration failed: $($_.Exception.Message)"
    if ($serviceChanged) {
        Set-ItemProperty -LiteralPath $serviceRegistryPath -Name ImagePath -Value $oldBinPath
    }
    if ((Get-Service -Name $ServiceName).Status -ne "Running") {
        Start-Service -Name $ServiceName
    }
    throw
} finally {
    Start-ScheduledTask -TaskName "Property Case Radar OpenAB Sidecar" -ErrorAction SilentlyContinue
    # The PDF broker is a gateway child process and may need a moment to
    # observe that its previous parent exited before releasing port 18766.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $broker = Get-NetTCPConnection -State Listen -LocalPort 18766 -ErrorAction SilentlyContinue
        if (-not $broker) {
            break
        }
        Start-Sleep -Milliseconds 500
    }
    Start-ScheduledTask -TaskName "Property Case Radar OpenAB Gateway" -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName "Property Case Radar Scheduler" -ErrorAction SilentlyContinue
}

Write-MigrationLog "migration completed; original C drive data retained for rollback"
Write-Output "PostgreSQL migration completed: $targetFull"
