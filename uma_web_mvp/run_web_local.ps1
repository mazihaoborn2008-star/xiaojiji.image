# ===== Local Test Startup Script =====
# Starts the UMA Web MVP in LOCAL TEST mode on port 8001
# Uses .env.local instead of .env — no production config is touched.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File run_web_local.ps1
#   OR
#   .\run_web_local.ps1

Set-Location $PSScriptRoot
chcp.com 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$env:APP_ENV = "local"

if (-not (Test-Path .env.local)) {
    Write-Host "❌ .env.local not found. Please create it first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path test_data\local_test.db)) {
    Write-Host "⚠️  Test database not found. Initializing..." -ForegroundColor Yellow
    python init_test_db.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Failed to initialize test database." -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  UMA Web MVP - LOCAL TEST MODE" -ForegroundColor Cyan
Write-Host "====================================" -ForegroundColor Cyan
Write-Host "  Port:     8001" -ForegroundColor Green
Write-Host "  DB:       test_data\local_test.db" -ForegroundColor Green
Write-Host "  Config:   .env.local" -ForegroundColor Green
Write-Host "  URL:      http://127.0.0.1:8001" -ForegroundColor Green
Write-Host "====================================" -ForegroundColor Cyan
Write-Host ""

python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --log-level info
