@echo off
cd /d "%~dp0"
set APP_ENV=local
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo [LOCAL MOCK WORKER] starting with .env.local
python -m app.mock.mock_generation_worker

