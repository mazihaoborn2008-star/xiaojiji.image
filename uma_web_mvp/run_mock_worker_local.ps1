$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:APP_ENV = "local"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Write-Host "[LOCAL MOCK WORKER] starting with .env.local"
python -m app.mock.mock_generation_worker

