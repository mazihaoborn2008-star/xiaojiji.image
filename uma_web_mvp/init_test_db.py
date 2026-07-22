"""
Initialize the local test database for UMA Web MVP.

Usage:
    set APP_ENV=local
    python init_test_db.py

This creates test_data/local_test.db with the same schema as production,
but with NO real user data. Safe to delete and recreate at any time.
"""
import os
import sys

# Force local test env before importing app config
os.environ["APP_ENV"] = "local"

# Ensure test_data directory exists
os.makedirs("test_data", exist_ok=True)
os.makedirs("test_data/output", exist_ok=True)
os.makedirs("test_data/mock_output", exist_ok=True)
os.makedirs("test_data/input_images", exist_ok=True)
os.makedirs("test_data/workflows", exist_ok=True)

# Import app modules after setting env
from app.config import get_settings, Settings
from app.db import ensure_schema

def init():
    settings = get_settings()
    print(f"[INIT] APP_ENV      = local")
    print(f"[INIT] BALANCE_DB   = {settings.balance_db}")
    print(f"[INIT] APP_ORIGIN   = {settings.app_origin}")
    print(f"[INIT] DEV_AUTH_BYPASS = {settings.dev_auth_bypass}")
    print(f"[INIT] COOKIE_SECURE   = {settings.cookie_secure}")

    ensure_schema(settings)
    print(f"[INIT] OK Test database initialized at {settings.balance_db}")

    # Verify
    import sqlite3
    db_path = str(settings.balance_db)
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    print(f"[INIT] Tables created: {', '.join(tables)}")
    print(f"[INIT] OK Local test DB ready. You can now run: run_web_local.ps1")

if __name__ == "__main__":
    init()
