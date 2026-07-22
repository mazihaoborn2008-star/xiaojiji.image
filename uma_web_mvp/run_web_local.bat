@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set APP_ENV=local
cd /d %~dp0

if not exist .env.local (
    echo ❌ .env.local not found.
    pause
    exit /b 1
)

if not exist test_data\local_test.db (
    echo ⚠ Test database not found. Initializing...
    python init_test_db.py
    if errorlevel 1 (
        echo ❌ Failed to initialize test database.
        pause
        exit /b 1
    )
)

echo.
echo ====================================
echo   UMA Web MVP - LOCAL TEST MODE
echo ====================================
echo   Port:     8001
echo   DB:       test_data\local_test.db
echo   Config:   .env.local
echo   URL:      http://127.0.0.1:8001
echo ====================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --log-level info
pause
