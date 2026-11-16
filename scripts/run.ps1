# Start NetAgent on Windows.  .\scripts\run.ps1 [-Port 8080]
param([int]$Port = 0, [string]$BindHost = "")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$extra = @()
if ($Port -gt 0) { $extra += @("--port", $Port) }
if ($BindHost) { $extra += @("--host", $BindHost) }
& .\.venv\Scripts\python.exe -m netagent serve @extra
