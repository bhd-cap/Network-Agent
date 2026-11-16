# Register NetAgent to start at boot on Windows using Task Scheduler (run from an elevated PowerShell).
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows-service.ps1
# Remove with:  Unregister-ScheduledTask -TaskName NetAgent -Confirm:$false
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts\install.ps1 first." }

$action = New-ScheduledTaskAction -Execute $python -Argument "-m netagent serve" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
# Runs as SYSTEM. For least privilege, replace with a dedicated service account that can read $root.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Limited
Register-ScheduledTask -TaskName "NetAgent" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "NetAgent"
Write-Host "NetAgent registered and started. Admin token:"
& $python -m netagent token
