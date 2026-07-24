"""Tests for recharge_requests schema compatibility.

Covers:
  A. Fresh database schema creation
  B. API on empty database
  C. Data lifecycle (create/query/update/cancel)
  D. Legacy database migration
  E. Old DB without recharge tables (auto-creation)
  F. Regression protection
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.db import ensure_schema, get_pending_topup_submit_reminder, list_admin_accounts, connect
from app.recharge_service import (
    cancel_recharge_request,
    connect_recharge_db,
    create_recharge_request_for_identity,
    ensure_recharge_schema,
    find_pending_topup_for_user,
    get_recharge_request,
    list_recharge_requests_for_user,
    mark_recharge_paid_for_user,
    normalize_payment_method,
)

TEST_USER = "test-recharge-user-001"
TEST_USER_B = "test-recharge-user-002"
TEST_USERNAME = "RechargeTestUser"


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _make_settings(tmp_path, **overrides):
    db_path = tmp_path / "test.db"
    input_dir = tmp_path / "input_images"
    output_dir = tmp_path / "output"
    mock_dir = tmp_path / "mock_output"
    for d in (input_dir, output_dir, mock_dir):
        d.mkdir(exist_ok=True)
    defaults = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:19999",
        "HOST": "127.0.0.1",
        "PORT": "19999",
        "BALANCE_DB": str(db_path),
        "BOT_DIR": str(tmp_path),
        "BOT_OUTPUT_DIR": str(output_dir),
        "mock_output_dir": str(mock_dir),
        "INPUT_IMAGE_DIR": str(input_dir),
        "COMFYUI_WORKFLOW_DIR": str(tmp_path / "workflows"),
        "SESSION_SECRET": "test_secret_recharge",
        "JWT_SECRET": "test_jwt_recharge",
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER,
        "dev_username": TEST_USERNAME,
        "MOCK_WORKER_ENABLED": "true",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_user(settings, user_id, balance_fen):
    conn = connect(settings)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES(?, ?)",
            (user_id, balance_fen),
        )
        conn.commit()
    finally:
        conn.close()


def _get_balance(settings, user_id):
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _create_recharge_request(db_path, user_id=TEST_USER, amount_fen=1000, username=TEST_USERNAME):
    return create_recharge_request_for_identity(
        db_path,
        user_id=user_id,
        username=username,
        amount_fen=amount_fen,
        payment_method="wechat_qr",
        source="test",
    )


# ────────────────────────────────────────────────────────────
# A. Fresh database schema
# ────────────────────────────────────────────────────────────

class TestFreshDatabaseSchema:
    """Verify ensure_recharge_schema creates all needed tables on a fresh DB."""

    def test_tables_created_on_fresh_db(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        ensure_recharge_schema(db_path)
        conn = connect_recharge_db(db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "recharge_requests" in tables
            assert "users" in tables
            assert "balance_ledger" in tables
        finally:
            conn.close()

    def test_recharge_requests_columns(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        ensure_recharge_schema(db_path)
        conn = connect_recharge_db(db_path)
        try:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(recharge_requests)"
            ).fetchall()}
            # Core columns from CREATE TABLE
            for col in ["code", "user_id", "username", "amount_fen", "status",
                        "created_at", "paid_at", "reviewed_at", "reviewer_id",
                        "reviewer_name", "note"]:
                assert col in columns, f"Missing column: {col}"
            # Columns added via ALTER TABLE migration
            for col in ["source", "owner_notify_status", "owner_notified_at",
                        "owner_notify_attempts", "owner_notify_error",
                        "payment_method", "payment_reference", "currency",
                        "credits", "expires_at", "paid_expires_at"]:
                assert col in columns, f"Missing migrated column: {col}"
        finally:
            conn.close()

    def test_indexes_created(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        ensure_recharge_schema(db_path)
        conn = connect_recharge_db(db_path)
        try:
            indexes = {row[1] for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            assert "idx_recharge_user_status" in indexes
            assert "idx_recharge_status_time" in indexes
            assert "idx_ledger_user_time" in indexes
        finally:
            conn.close()

    def test_ensure_recharge_schema_idempotent(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        ensure_recharge_schema(db_path)
        ensure_recharge_schema(db_path)  # second call must not fail
        conn = connect_recharge_db(db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "recharge_requests" in tables
        finally:
            conn.close()

    def test_no_such_table_error_resolved(self, tmp_path):
        """The original error: querying recharge_requests on fresh DB must not fail."""
        db_path = tmp_path / "fresh.db"
        ensure_recharge_schema(db_path)
        # This was the failing query path
        result = list_recharge_requests_for_user(db_path, TEST_USER)
        assert result == []

    def test_full_schema_via_both_init_paths(self, tmp_path):
        """ensure_schema (db.py) + ensure_recharge_schema together create all tables."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)
        conn = connect(settings)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            # From ensure_schema
            assert "users" in tables
            assert "generation_tasks" in tables
            assert "accounts" in tables
            # From ensure_recharge_schema
            assert "recharge_requests" in tables
            assert "balance_ledger" in tables
        finally:
            conn.close()


# ────────────────────────────────────────────────────────────
# B. API on empty database
# ────────────────────────────────────────────────────────────

class TestAPIOnEmptyDatabase:
    """Verify recharge-related API endpoints return valid responses on empty DB."""

    def test_list_topups_empty(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/topups")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert data["items"] == []
        finally:
            app.dependency_overrides.clear()

    def test_create_topup_on_empty_db(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/topups", json={
                "amount_rmb": "10.00",
                "payment_method": "wechat_qr",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert "item" in data
            assert data["item"]["code"] is not None
            assert data["item"]["status"] == "created"
        finally:
            app.dependency_overrides.clear()

    def test_topup_page_returns_200(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/topup", follow_redirects=False)
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_home_page_no_recharge_error(self, tmp_path):
        """Home page must not trigger no such table for recharge_requests."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/")
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.clear()

    def test_pending_submit_reminder_empty(self, tmp_path):
        """get_pending_topup_submit_reminder must not fail on fresh DB."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        conn = connect(settings)
        try:
            result = get_pending_topup_submit_reminder(conn, legacy_user_id=TEST_USER)
            assert result is None
        finally:
            conn.close()

    def test_admin_accounts_no_recharge_error(self, tmp_path):
        """list_admin_accounts LEFT JOINs recharge_requests — must not fail."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        conn = connect(settings)
        try:
            result = list_admin_accounts(conn)
            assert isinstance(result, list)
        finally:
            conn.close()


# ────────────────────────────────────────────────────────────
# C. Data lifecycle
# ────────────────────────────────────────────────────────────

class TestRechargeDataLifecycle:
    """Verify full lifecycle: create → query → update → cancel."""

    def test_create_and_query(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        code = _create_recharge_request(settings.balance_db)
        assert code is not None
        assert len(code) > 0

        order = get_recharge_request(settings.balance_db, code)
        assert order is not None
        assert order["user_id"] == TEST_USER
        assert order["amount_fen"] == 1000
        assert order["status"] == "created"

    def test_list_for_user(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        _create_recharge_request(settings.balance_db, amount_fen=1000)
        _create_recharge_request(settings.balance_db, amount_fen=2000)

        items = list_recharge_requests_for_user(settings.balance_db, TEST_USER)
        assert len(items) == 2
        amounts = {item["amount_fen"] for item in items}
        assert amounts == {1000, 2000}

    def test_find_pending_topup(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        _create_recharge_request(settings.balance_db)
        pending = find_pending_topup_for_user(settings.balance_db, TEST_USER)
        assert pending is not None
        assert pending["status"] == "created"

    def test_cancel_request(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        code = _create_recharge_request(settings.balance_db)
        ok, msg = cancel_recharge_request(settings.balance_db, code, TEST_USER)
        assert ok is True

        order = get_recharge_request(settings.balance_db, code)
        assert order["status"] == "cancelled"

    def test_amount_consistency(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        code = _create_recharge_request(settings.balance_db, amount_fen=5000)
        order = get_recharge_request(settings.balance_db, code)
        assert order["amount_fen"] == 5000
        # credits defaults to amount_fen
        assert order["credits"] == 5000

    def test_user_binding_correct(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        code_a = _create_recharge_request(settings.balance_db, user_id=TEST_USER, amount_fen=1000)
        code_b = _create_recharge_request(settings.balance_db, user_id=TEST_USER_B, amount_fen=2000)

        items_a = list_recharge_requests_for_user(settings.balance_db, TEST_USER)
        items_b = list_recharge_requests_for_user(settings.balance_db, TEST_USER_B)
        assert len(items_a) == 1
        assert len(items_b) == 1
        assert items_a[0]["amount_fen"] == 1000
        assert items_b[0]["amount_fen"] == 2000

    def test_request_code_unique(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        codes = set()
        # MAX_PENDING_TOPUPS_PER_USER is 3, so create 3 then cancel 1 to make room
        for i in range(5):
            code = _create_recharge_request(settings.balance_db)
            codes.add(code)
            # Cancel to stay under limit
            if len(codes) >= 3:
                cancel_recharge_request(settings.balance_db, code, TEST_USER)
        assert len(codes) == 5

    def test_payment_method_normalized(self, tmp_path):
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        code = _create_recharge_request(settings.balance_db)
        order = get_recharge_request(settings.balance_db, code)
        assert order["payment_method"] == "wechat_qr"


# ────────────────────────────────────────────────────────────
# D. Legacy database migration
# ────────────────────────────────────────────────────────────

class TestLegacyDatabaseMigration:
    """Verify migration from old schema preserves data."""

    def _create_legacy_db(self, db_path):
        """Create a database with the original recharge_requests schema (no migration columns)."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                balance_fen INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS recharge_requests (
                code TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                amount_fen INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                created_at INTEGER NOT NULL,
                paid_at INTEGER,
                reviewed_at INTEGER,
                reviewer_id TEXT,
                reviewer_name TEXT,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_recharge_user_status ON recharge_requests(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_recharge_status_time ON recharge_requests(status, created_at);
        """)
        now = int(time.time())
        # Insert test records
        conn.execute(
            "INSERT INTO recharge_requests(code, user_id, username, amount_fen, status, created_at) VALUES(?,?,?,?,?,?)",
            ("RC-LEGACY-001", TEST_USER, TEST_USERNAME, 1000, "created", now - 3600),
        )
        conn.execute(
            "INSERT INTO recharge_requests(code, user_id, username, amount_fen, status, created_at, paid_at) VALUES(?,?,?,?,?,?,?)",
            ("RC-LEGACY-002", TEST_USER, TEST_USERNAME, 2000, "paid", now - 7200, now - 3000),
        )
        conn.execute(
            "INSERT INTO recharge_requests(code, user_id, username, amount_fen, status, created_at) VALUES(?,?,?,?,?,?)",
            ("RC-LEGACY-003", TEST_USER_B, "OtherUser", 500, "cancelled", now - 86400),
        )
        conn.execute(
            "INSERT INTO recharge_requests(code, user_id, username, amount_fen, status, created_at) VALUES(?,?,?,?,?,?)",
            ("RC-LEGACY-004", TEST_USER, TEST_USERNAME, 3000, "created", now - 120),
        )
        conn.commit()
        conn.close()

    def test_migration_preserves_data(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        # Record pre-migration state
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        pre_rows = conn.execute("SELECT * FROM recharge_requests ORDER BY code").fetchall()
        pre_count = len(pre_rows)
        pre_data = {row["code"]: dict(row) for row in pre_rows}
        conn.close()

        # Run migration
        ensure_recharge_schema(db_path)

        # Verify post-migration
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        post_rows = conn.execute("SELECT * FROM recharge_requests ORDER BY code").fetchall()
        post_count = len(post_rows)
        post_data = {row["code"]: dict(row) for row in post_rows}
        conn.close()

        assert post_count == pre_count, f"Row count changed: {pre_count} -> {post_count}"
        for code in pre_data:
            assert code in post_data, f"Missing record: {code}"
            for field in pre_data[code]:
                assert post_data[code][field] == pre_data[code][field], (
                    f"Field {field} changed for {code}: {pre_data[code][field]} -> {post_data[code][field]}"
                )

    def test_migration_adds_new_columns(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)
        ensure_recharge_schema(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recharge_requests)").fetchall()}
        conn.close()

        # New columns should exist after migration
        for col in ["payment_method", "expires_at", "currency", "credits",
                     "source", "owner_notify_status"]:
            assert col in columns, f"Migration did not add column: {col}"

    def test_migration_idempotent(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)

        ensure_recharge_schema(db_path)
        ensure_recharge_schema(db_path)  # second run

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM recharge_requests").fetchall()
        conn.close()
        assert len(rows) == 4

    def test_migration_populates_defaults(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        self._create_legacy_db(db_path)
        ensure_recharge_schema(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM recharge_requests").fetchall():
            row = dict(row)
            assert row["payment_method"] is not None and row["payment_method"] != ""
            assert row["currency"] is not None and row["currency"] != ""
            assert row["credits"] is not None
            assert row["expires_at"] is not None
        conn.close()


# ────────────────────────────────────────────────────────────
# E. Old DB without recharge tables
# ────────────────────────────────────────────────────────────

class TestOldDBWithoutRechargeTables:
    """DB with users/generation_tasks but no recharge tables — auto-creation."""

    def _create_partial_db(self, db_path):
        """Create a DB with users and generation_tasks matching the current schema (but no recharge tables)."""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                balance_fen INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS generation_tasks (
                job_code TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                channel_id TEXT,
                prompt TEXT NOT NULL,
                original_prompt TEXT,
                effective_prompt TEXT,
                use_agent INTEGER NOT NULL DEFAULT 0,
                agent_mode TEXT NOT NULL DEFAULT 'normal',
                style_key TEXT NOT NULL DEFAULT 'default',
                lora_weight REAL NOT NULL DEFAULT 1.0,
                width INTEGER NOT NULL DEFAULT 1024,
                height INTEGER NOT NULL DEFAULT 1536,
                generation_mode TEXT NOT NULL DEFAULT 'txt2img',
                denoise REAL NOT NULL DEFAULT 0.5,
                control_type TEXT NOT NULL DEFAULT 'depth',
                control_character TEXT NOT NULL DEFAULT 'prompt',
                auto_tagger INTEGER NOT NULL DEFAULT 0,
                charged_fen INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                translation_mode TEXT NOT NULL DEFAULT 'none',
                fast_translation_request_code TEXT NOT NULL DEFAULT '',
                request_fingerprint TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'discord'
            );
        """)
        now = int(time.time())
        conn.execute("INSERT INTO users(user_id, balance_fen) VALUES(?, ?)", (TEST_USER, 50000))
        conn.execute(
            "INSERT INTO generation_tasks(job_code, user_id, username, prompt, charged_fen, status, created_at) VALUES(?,?,?,?,?,?,?)",
            ("JOB-001", TEST_USER, TEST_USERNAME, "test prompt", 1000, "done", now),
        )
        conn.commit()
        conn.close()

    def test_auto_creates_recharge_tables(self, tmp_path):
        db_path = tmp_path / "partial.db"
        self._create_partial_db(db_path)

        ensure_recharge_schema(db_path)

        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        assert "recharge_requests" in tables
        assert "balance_ledger" in tables

    def test_existing_data_preserved(self, tmp_path):
        db_path = tmp_path / "partial.db"
        self._create_partial_db(db_path)

        ensure_recharge_schema(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (TEST_USER,)).fetchone()
        assert user is not None
        assert user["balance_fen"] == 50000

        task = conn.execute("SELECT * FROM generation_tasks WHERE job_code=?", ("JOB-001",)).fetchone()
        assert task is not None
        assert task["prompt"] == "test prompt"
        conn.close()

    def test_can_use_recharge_after_auto_create(self, tmp_path):
        db_path = tmp_path / "partial.db"
        self._create_partial_db(db_path)

        ensure_recharge_schema(db_path)

        # Now use recharge functions
        code = _create_recharge_request(db_path, amount_fen=2500)
        order = get_recharge_request(db_path, code)
        assert order is not None
        assert order["amount_fen"] == 2500

    def test_list_admin_accounts_works(self, tmp_path):
        """After auto-creating recharge tables, list_admin_accounts must work."""
        settings = _make_settings(tmp_path)
        # Create partial DB first
        self._create_partial_db(settings.balance_db)
        # Now run full schema init
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        conn = connect(settings)
        try:
            result = list_admin_accounts(conn)
            assert isinstance(result, list)
        finally:
            conn.close()


# ────────────────────────────────────────────────────────────
# F. Regression: existing tests still pass
# ────────────────────────────────────────────────────────────

class TestRegressionProtection:
    """Ensure the recharge schema fix doesn't break existing functionality."""

    def test_generation_task_lifecycle(self, tmp_path):
        """Generation task creation still works with recharge schema present."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        _seed_user(settings, TEST_USER, 50000)
        conn = connect(settings)
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO generation_tasks(job_code, user_id, username, channel_id, prompt, original_prompt, effective_prompt, use_agent, agent_mode, style_key, loras_json, charged_fen, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("JOB-REG-001", TEST_USER, TEST_USERNAME, "test-ch", "test prompt", "test prompt", "test prompt", 0, "normal", "default", "[]", 1000, "pending", now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM generation_tasks WHERE job_code=?", ("JOB-REG-001",)).fetchone()
            assert row is not None
            assert row["status"] == "pending"
        finally:
            conn.close()

    def test_cancellation_refund(self, tmp_path):
        """Task cancellation with refund still works."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)
        _seed_user(settings, TEST_USER, 50000)

        conn = connect(settings)
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO generation_tasks(job_code, user_id, username, channel_id, prompt, original_prompt, effective_prompt, use_agent, agent_mode, style_key, loras_json, charged_fen, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("JOB-CANCEL-001", TEST_USER, TEST_USERNAME, "test-ch", "test", "test", "test", 0, "normal", "default", "[]", 1000, "translating", now),
            )
            conn.commit()

            from app.db import cancel_task_atomic
            result = cancel_task_atomic(settings, TEST_USER, "JOB-CANCEL-001")
            assert result["job_code"] == "JOB-CANCEL-001"
            assert "cancelled" in result["status"]
            assert result["refunded_fen"] > 0
            assert result["already_cancelled"] is False
        finally:
            conn.close()

    def test_recharge_and_generation_independent(self, tmp_path):
        """Recharge operations don't interfere with generation tasks."""
        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        ensure_recharge_schema(settings.balance_db)

        # Create a generation task
        conn = connect(settings)
        try:
            now = int(time.time())
            conn.execute(
                "INSERT INTO generation_tasks(job_code, user_id, username, channel_id, prompt, original_prompt, effective_prompt, use_agent, agent_mode, style_key, loras_json, charged_fen, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("JOB-COEXIST-001", TEST_USER, TEST_USERNAME, "test-ch", "test", "test", "test", 0, "normal", "default", "[]", 1000, "pending", now),
            )
            conn.commit()
        finally:
            conn.close()

        # Create a recharge request
        code = _create_recharge_request(settings.balance_db, amount_fen=1000)
        order = get_recharge_request(settings.balance_db, code)
        assert order is not None

        # Verify generation task still exists
        conn = connect(settings)
        try:
            row = conn.execute("SELECT * FROM generation_tasks WHERE job_code=?", ("JOB-COEXIST-001",)).fetchone()
            assert row is not None
        finally:
            conn.close()
