$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:APP_ENV = "local"
Write-Host "[LOCAL TEST] Starting web on 127.0.0.1:8001"
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "run_web_local.ps1")
Start-Sleep -Seconds 2
Write-Host "[LOCAL TEST] Starting mock worker"
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "run_mock_worker_local.ps1")
Write-Host "[LOCAL TEST] Open http://127.0.0.1:8001"

