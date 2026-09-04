$ErrorActionPreference = "Stop"

$registrations = @(
    "register_api_task.ps1",
    "register_scheduler_task.ps1",
    "register_umi_ocr_task.ps1",
    "register_openab_tasks.ps1",
    "register_database_backup_task.ps1",
    "register_scheduler_self_healer_task.ps1",
    "register_system_watchdog_task.ps1"
)

foreach ($registration in $registrations) {
    & (Join-Path $PSScriptRoot $registration)
}

Get-ScheduledTask | Where-Object { $_.TaskName -like "Property Case Radar*" } |
    Select-Object TaskName, State |
    Sort-Object TaskName
