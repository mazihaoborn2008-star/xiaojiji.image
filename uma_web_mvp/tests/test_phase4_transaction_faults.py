"""Phase 4: Transaction commit failure fault injection tests.

Tests that commit() failures cause complete rollback of all changes.
Uses _FailingConnection to inject OperationalError at commit() calls.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("APP_ENV", "local")

from app.config import Settings
from app.db import (
    SMART_AGENT_CHARGE_REASON,
    SMART_AGENT_REFUND_REASON,
    connect,
    create_smart_agent_task_atomic,
    create_smart_agent_queued_task_atomic,
    ensure_schema,
    fail_smart_agent_task_refund,
)

TEST_USER = "commit-fault-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "commit_fault_cases"


def _case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_settings(case_root: Path, **overrides) -> Settings:
    test_root = case_root / "test_data"
    for d in ("output", "mock_output", "input_images"):
        (test_root / d).mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(test_root / "commit_fault_test.db"),
        "BOT_OUTPUT_DIR": str(test_root / "output"),
        "mock_output_dir": str(test_root / "mock_output"),
        "INPUT_IMAGE_DIR": str(test_root / "input_images"),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER,
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "smart_agent_cost_credits": 5,
        "price_fen_per_image": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "",
        "session_secret": "commit-fault-session-secret-32chars!",
        "jwt_secret": "commit-fault-jwt-secret-32chars!!!!",
        "agent_enabled": False,
        "smart_agent_enabled": False,
        "max_queue_size": 100,
        "max_active_tasks_per_user": 10,
    }
    data.update(overrides)
    s = Settings(**data)
    s.validate_local_isolation()
    ensure_schema(s)
    return s


def _seed_balance(settings: Settings, user_id: str, amount: int = 50000) -> None:
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (user_id, amount))
        conn.commit()
    finally:
        conn.close()


def _get_balance(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance_fen"]) if row else 0
    finally:
        conn.close()


def _count(settings: Settings, table: str, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


class _CommitFailingConnection:
    """Wrapper that fails commit() but allows rollback()."""

    def __init__(self, conn, fail_commits=1):
        self._conn = conn
        self._fail_commits = fail_commits
        self._commit_count = 0

    def execute(self, sql, *args, **kwargs):
        return self._conn.execute(sql, *args, **kwargs)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        self._commit_count += 1
        if self._commit_count <= self._fail_commits:
            raise sqlite3.OperationalError("Injected commit failure")
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Creation Commit Failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCreationCommitFailure:
    """Verify commit failure rolls back all creation changes."""

    def test_commit_failure_rolls_back_smart_agent_task(self):
        """commit() failure should roll back balance, task, and idempotency."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        cid = f"commit-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _CommitFailingConnection(orig(s))
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError, match="commit"):
                create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                    client_request_id=cid,
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count(settings, "generation_tasks", TEST_USER) == 0
        assert _count(settings, "smart_agent_request_idempotency", TEST_USER) == 0

    def test_commit_failure_rolls_back_queued_task(self):
        """commit() failure on queued task should roll back everything."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        cid = f"qcommit-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _CommitFailingConnection(orig(s))
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError, match="commit"):
                create_smart_agent_queued_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test request", cost_credits=5,
                    plan_json="{}", prompt="test prompt",
                    workflow_key="default", loras_json="[]",
                    prompt_source="smart_agent", width=1024, height=1536,
                    client_request_id=cid,
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count(settings, "generation_tasks", TEST_USER) == 0
        assert _count(settings, "smart_agent_request_idempotency", TEST_USER) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Refund Commit Failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRefundCommitFailure:
    """Verify commit failure rolls back all refund changes."""

    def test_commit_failure_rolls_back_refund(self):
        """commit() failure during refund should roll back everything."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        bal_after_charge = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _CommitFailingConnection(orig(s))
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError, match="commit"):
                fail_smart_agent_task_refund(settings, job_code=job_code, error="test")
        finally:
            db_mod.connect = orig

        # Everything should be rolled back
        assert _get_balance(settings, TEST_USER) == bal_after_charge
        assert _count(settings, "smart_agent_billing_events", TEST_USER) == 0
        conn = connect(settings)
        try:
            row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
            assert row["status"] == "smart_planning"
            ledger = conn.execute(
                "SELECT COUNT(*) as c FROM balance_ledger WHERE user_id=? AND reason=?",
                (TEST_USER, SMART_AGENT_REFUND_REASON),
            ).fetchone()
            assert ledger["c"] == 0
        finally:
            conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
