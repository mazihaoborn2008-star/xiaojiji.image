"""Phase 4 followup: fingerprint completeness, cross-reason refund, confirm idempotency, fault injection.

Verifies:
1. Fingerprint covers all business-affecting fields (workflow_key, character_key, etc.)
2. Same task cannot be refunded via different reason codes
3. confirm_smart_agent_prompt_draft_atomic uses idempotency table
4. Transaction failures leave no partial state (explicit fault injection)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    _check_idempotency,
    _compute_request_fingerprint,
    _insert_refund_event,
    connect,
    create_smart_agent_task_atomic,
    ensure_schema,
    fail_smart_agent_task_refund,
)

TEST_USER = "phase4-followup-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "phase4_followup_cases"


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
        "BALANCE_DB": str(test_root / "followup_test.db"),
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
        "session_secret": "followup-session-secret-32chars!!!!!",
        "jwt_secret": "followup-jwt-secret-32chars!!!!!!!!!!",
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


def _count_table(settings: Settings, table: str, user_id: str, where: str = "") -> int:
    conn = connect(settings)
    try:
        sql = f"SELECT COUNT(*) FROM {table} WHERE user_id=?"
        if where:
            sql += f" AND {where}"
        row = conn.execute(sql, (user_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _count_billing_events(settings: Settings, user_id: str) -> int:
    return _count_table(settings, "smart_agent_billing_events", user_id)


def _count_idempotency(settings: Settings, user_id: str) -> int:
    return _count_table(settings, "smart_agent_request_idempotency", user_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Fingerprint Completeness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFingerprintCompleteness:
    """Verify fingerprint covers all business-affecting fields."""

    def test_different_workflow_key_different_fingerprint(self):
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, workflow_key="wf_a")
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, workflow_key="wf_b")
        assert fp1 != fp2

    def test_different_character_key_different_fingerprint(self):
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, character_keys=["char_a"])
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, character_keys=["char_b"])
        assert fp1 != fp2

    def test_different_prompt_source_different_fingerprint(self):
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, prompt_source="smart_agent")
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, prompt_source="user_raw")
        assert fp1 != fp2

    def test_different_dimensions_different_fingerprint(self):
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, width=1024, height=1536)
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, width=512, height=512)
        assert fp1 != fp2

    def test_character_key_order_normalized(self):
        """Character keys should be sorted for stable comparison."""
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, character_keys=["a", "b"])
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, character_keys=["b", "a"])
        assert fp1 == fp2

    def test_same_payload_same_fingerprint(self):
        fp1 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, workflow_key="wf", character_keys=["c"])
        fp2 = _compute_request_fingerprint(user_id="u1", request_text="t", cost_credits=5, workflow_key="wf", character_keys=["c"])
        assert fp1 == fp2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Cross-Reason Refund Protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCrossReasonRefundProtection:
    """Verify same task cannot get multiple refunds via different reasons."""

    def test_event_key_does_not_include_reason(self):
        """Refund event key must be task-only, not task+reason."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            _insert_refund_event(conn, user_id="u", task_id="JOB-1", amount=5, reason="timeout", now=1000)
            conn.commit()
            row = conn.execute("SELECT event_key FROM smart_agent_billing_events WHERE task_id='JOB-1'").fetchone()
            assert "timeout" not in row["event_key"]
            assert "JOB-1" in row["event_key"]
        finally:
            conn.close()

    def test_different_reason_same_task_only_one_refund(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            r1 = _insert_refund_event(conn, user_id="u", task_id="JOB-2", amount=5, reason="timeout", now=1000)
            conn.commit()
            r2 = _insert_refund_event(conn, user_id="u", task_id="JOB-2", amount=5, reason="deepseek_error", now=1001)
            conn.commit()
            assert r1 is True
            assert r2 is False  # Duplicate key
        finally:
            conn.close()

    def test_concurrent_refund_same_task_only_one(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        results = []
        barrier = threading.Barrier(3, timeout=10)

        def _insert(reason):
            conn = connect(settings)
            try:
                barrier.wait()
                r = _insert_refund_event(conn, user_id="u", task_id="JOB-3", amount=5, reason=reason, now=1000)
                conn.commit()
                results.append(r)
            finally:
                conn.close()

        threads = [threading.Thread(target=_insert, args=(f"reason_{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert sum(1 for r in results if r is True) == 1

    def test_full_refund_flow_different_reason_only_once(self):
        """End-to-end: same task, different reasons, only one refund."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        assert _get_balance(settings, TEST_USER) == 49995

        r1 = fail_smart_agent_task_refund(settings, job_code=job_code, error="timeout")
        assert r1 is True
        assert _get_balance(settings, TEST_USER) == 50000

        r2 = fail_smart_agent_task_refund(settings, job_code=job_code, error="different_error")
        assert r2 is False
        assert _get_balance(settings, TEST_USER) == 50000
        assert _count_billing_events(settings, TEST_USER) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. confirm_smart_agent_prompt_draft_atomic Idempotency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfirmDraftIdempotency:
    """Verify confirm function uses idempotency table."""

    def _create_draft(self, settings, conversation_id=900):
        conn = connect(settings)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO smart_agent_prompt_drafts "
                "(conversation_id, message_id, prompt_draft, plan_json, request_text, "
                "workflow_key, loras_json, prompt_source, resolved_character_key, workflow_source, "
                "fallback_level, width, height, prompt_version, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, 0, "test prompt", "{}", "test request",
                 "default", "[]", "smart_agent", "", "", "",
                 1024, 1536, 1, "prompt_ready", 1000, 1000),
            )
            conn.commit()
        finally:
            conn.close()

    def test_confirm_writes_idempotency_record(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        self._create_draft(settings, 901)

        from app.db import confirm_smart_agent_prompt_draft_atomic
        result = confirm_smart_agent_prompt_draft_atomic(
            settings, conversation_id=901, user_id=TEST_USER,
            username="tester", job_code=f"JOB-{uuid.uuid4().hex[:8]}", cost_credits=5,
        )
        assert result["job_code"]
        # Should have written to idempotency table
        assert _count_idempotency(settings, TEST_USER) >= 1

    def test_confirm_double_returns_existing(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        self._create_draft(settings, 902)

        from app.db import confirm_smart_agent_prompt_draft_atomic
        r1 = confirm_smart_agent_prompt_draft_atomic(
            settings, conversation_id=902, user_id=TEST_USER,
            username="tester", job_code=f"JOB-{uuid.uuid4().hex[:8]}", cost_credits=5,
        )
        balance_after = _get_balance(settings, TEST_USER)

        r2 = confirm_smart_agent_prompt_draft_atomic(
            settings, conversation_id=902, user_id=TEST_USER,
            username="tester", job_code=f"JOB-{uuid.uuid4().hex[:8]}", cost_credits=5,
        )
        assert r2.get("already_created") is True
        assert _get_balance(settings, TEST_USER) == balance_after


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Transaction Fault Injection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FailingConnection:
    """Wrapper that can inject failures at specific SQL patterns."""

    def __init__(self, conn, fail_patterns=None):
        self._conn = conn
        self._fail_patterns = fail_patterns or []
        self._fail_counts = {}

    def execute(self, sql, *args, **kwargs):
        sql_str = str(sql)
        for pattern, max_fails in self._fail_patterns:
            if pattern in sql_str:
                count = self._fail_counts.get(pattern, 0)
                if count < max_fails:
                    self._fail_counts[pattern] = count + 1
                    raise sqlite3.OperationalError(f"Injected failure: {pattern}")
        return self._conn.execute(sql, *args, **kwargs)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestTaskCreationFaultInjection:
    """Fault injection for task creation transaction."""

    def test_balance_deduction_failure_rolls_back_everything(self):
        """If balance deduction fails, idempotency record and task should not exist."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        client_id = f"fault-bal-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE users SET balance_fen = balance_fen -", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                    client_request_id=client_id,
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count_table(settings, "generation_tasks", TEST_USER) == 0
        assert _count_idempotency(settings, TEST_USER) == 0

    def test_task_insert_failure_rolls_back_everything(self):
        """If task INSERT fails, balance and idempotency should be rolled back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        client_id = f"fault-ins-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("INSERT INTO generation_tasks", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                    client_request_id=client_id,
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count_table(settings, "generation_tasks", TEST_USER) == 0
        assert _count_idempotency(settings, TEST_USER) == 0


class TestRefundFaultInjection:
    """Fault injection for refund transaction."""

    def test_balance_update_failure_rolls_back_refund(self):
        """If balance update fails during refund, billing event should be rolled back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        balance_after = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE users SET balance_fen=balance_fen+", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(settings, job_code=job_code, error="test")
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == balance_after
        assert _count_billing_events(settings, TEST_USER) == 0
        conn = connect(settings)
        try:
            row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
            assert row["status"] == "smart_planning"
        finally:
            conn.close()

    def test_ledger_insert_failure_rolls_back_refund(self):
        """If balance_ledger INSERT fails, everything should roll back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        balance_after = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("INSERT INTO balance_ledger", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(settings, job_code=job_code, error="test")
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == balance_after
        assert _count_billing_events(settings, TEST_USER) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
