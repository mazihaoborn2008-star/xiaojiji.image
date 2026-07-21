@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d %~dp0
if not exist .env copy .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
