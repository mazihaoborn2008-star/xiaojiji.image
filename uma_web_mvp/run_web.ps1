Set-Location $PSScriptRoot
chcp.com 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
