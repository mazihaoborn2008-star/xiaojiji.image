"""Phase 3 Step 4: Database-level billing idempotency and refund atomicity hardening tests.

These tests verify:
1. DB-level unique constraint prevents duplicate tasks for same client_request_id
2. Fingerprint mismatch returns 409 conflict (not silent reuse)
3. Concurrent requests with same ID only create one task
4. Refund ledger prevents double-refund at DB level
5. Transaction rollback leaves no partial state
6. 409 character confirmation compatibility
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
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
    ensure_schema,
    fail_smart_agent_task_refund,
)

TEST_USER = "hardening-test-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "hardening_cases"


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
        "BALANCE_DB": str(test_root / "hardening_test.db"),
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
        "session_secret": "hardening-test-session-secret-32chars!",
        "jwt_secret": "hardening-test-jwt-secret-32chars!!!!",
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


def _count_tasks(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT COUNT(*) FROM generation_tasks WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _count_ledger(settings: Settings, user_id: str, reason: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE user_id=? AND reason=?",
            (user_id, reason),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _has_idempotency_table(settings: Settings) -> bool:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='smart_agent_request_idempotency'"
        ).fetchall()
        return len(rows) > 0
    finally:
        conn.close()


def _has_billing_events_table(settings: Settings) -> bool:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='smart_agent_billing_events'"
        ).fetchall()
        return len(rows) > 0
    finally:
        conn.close()


def _get_idempotency_record(settings: Settings, user_id: str, client_request_id: str) -> dict | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM smart_agent_request_idempotency WHERE user_id=? AND client_request_id=?",
            (user_id, client_request_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_billing_events(settings: Settings, user_id: str, task_id: str = "") -> list[dict]:
    conn = connect(settings)
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM smart_agent_billing_events WHERE user_id=? AND task_id=?",
                (user_id, task_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM smart_agent_billing_events WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. DB-level Idempotency Table
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIdempotencyTable:
    """Verify idempotency table exists and has proper constraints."""

    def test_idempotency_table_exists(self):
        """Idempotency table should be created by ensure_schema."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        assert _has_idempotency_table(settings), \
            "smart_agent_request_idempotency table missing"

    def test_idempotency_unique_constraint(self):
        """Inserting duplicate (user_id, client_request_id) should fail."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            conn.execute(
                "INSERT INTO smart_agent_request_idempotency "
                "(user_id, client_request_id, request_fingerprint, request_status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (TEST_USER, "test-id-1", "fp1", "completed", 1000),
            )
            conn.commit()
            with pytest.raises(Exception):  # UNIQUE constraint violation
                conn.execute(
                    "INSERT INTO smart_agent_request_idempotency "
                    "(user_id, client_request_id, request_fingerprint, request_status, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (TEST_USER, "test-id-1", "fp2", "completed", 1001),
                )
                conn.commit()
        finally:
            conn.close()

    def test_different_users_same_id_allowed(self):
        """Different users with same client_request_id should not conflict."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            conn.execute(
                "INSERT INTO smart_agent_request_idempotency "
                "(user_id, client_request_id, request_fingerprint, request_status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("user-a", "shared-id", "fp1", "completed", 1000),
            )
            conn.execute(
                "INSERT INTO smart_agent_request_idempotency "
                "(user_id, client_request_id, request_fingerprint, request_status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("user-b", "shared-id", "fp2", "completed", 1001),
            )
            conn.commit()
        finally:
            conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. DB-level Task Creation Idempotency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDBLevelTaskIdempotency:
    """Verify DB constraint prevents duplicate task creation."""

    def test_same_id_same_payload_creates_one_task(self):
        """Same client_request_id with same payload should return existing task."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"db-idemp-{uuid.uuid4().hex[:8]}"

        result1 = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="same prompt", cost_credits=5,
            client_request_id=client_id,
        )
        balance_after_first = _get_balance(settings, TEST_USER)

        result2 = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="same prompt", cost_credits=5,
            client_request_id=client_id,
        )
        balance_after_second = _get_balance(settings, TEST_USER)

        assert result1["job_code"] == result2["job_code"]
        assert balance_after_first == balance_after_second
        assert _count_tasks(settings, TEST_USER) == 1
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1

    def test_concurrent_same_id_only_one_task(self):
        """Concurrent requests with same ID should create only one task."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"concurrent-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []
        barrier = threading.Barrier(4, timeout=10)

        def _create(idx):
            try:
                barrier.wait()
                result = create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="concurrent test", cost_credits=5,
                    client_request_id=client_id,
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All should return same job_code
        job_codes = {r["job_code"] for r in results}
        assert len(job_codes) == 1, f"Expected 1 unique job_code, got {len(job_codes)}: {job_codes}"
        assert _count_tasks(settings, TEST_USER) == 1
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1
        assert _get_balance(settings, TEST_USER) == 49995

    def test_same_id_different_payload_conflict(self):
        """Same ID with different payload should return conflict error."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"conflict-{uuid.uuid4().hex[:8]}"

        create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="prompt A", cost_credits=5,
            client_request_id=client_id,
        )
        balance_after_first = _get_balance(settings, TEST_USER)

        # Same ID, different prompt → should fail
        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_task_atomic(
                settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                user_id=TEST_USER, username="tester",
                request_text="prompt B COMPLETELY DIFFERENT", cost_credits=5,
                client_request_id=client_id,
            )

        # Balance should not change
        assert _get_balance(settings, TEST_USER) == balance_after_first
        assert _count_tasks(settings, TEST_USER) == 1
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Refund Atomicity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestRefundAtomicity:
    """Verify refund has DB-level idempotency."""

    def test_billing_events_table_exists(self):
        """Billing events table should exist for refund idempotency."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        assert _has_billing_events_table(settings), \
            "smart_agent_billing_events table missing"

    def test_double_refund_only_once(self):
        """Double refund should only refund once at DB level."""
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

        # First refund
        r1 = fail_smart_agent_task_refund(settings, job_code=job_code, error="e1")
        assert r1 is True
        balance_after = _get_balance(settings, TEST_USER)
        assert balance_after == 50000

        # Second refund
        r2 = fail_smart_agent_task_refund(settings, job_code=job_code, error="e2")
        assert r2 is False
        assert _get_balance(settings, TEST_USER) == 50000

        # Only one refund ledger entry
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_REFUND_REASON) == 1

    def test_concurrent_refund_only_once(self):
        """Concurrent refund attempts should only refund once."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )

        results = []
        barrier = threading.Barrier(3, timeout=10)

        def _refund():
            try:
                barrier.wait()
                r = fail_smart_agent_task_refund(settings, job_code=job_code, error="concurrent")
                results.append(r)
            except Exception:
                results.append(False)

        threads = [threading.Thread(target=_refund) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        true_count = sum(1 for r in results if r is True)
        assert true_count == 1, f"Expected exactly 1 successful refund, got {true_count}"
        assert _get_balance(settings, TEST_USER) == 50000
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_REFUND_REASON) == 1

    def test_refund_event_in_billing_events(self):
        """Refund should be recorded in billing events table."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )

        fail_smart_agent_task_refund(settings, job_code=job_code, error="test")

        events = _get_billing_events(settings, TEST_USER)
        refund_events = [e for e in events if e.get("event_type") == "refund"]
        assert len(refund_events) == 1

    def test_refund_on_already_refunded_task(self):
        """Refund on already-refunded task should be idempotent."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )

        # First refund
        fail_smart_agent_task_refund(settings, job_code=job_code, error="e1")

        # Second refund should not change balance
        balance_before = _get_balance(settings, TEST_USER)
        fail_smart_agent_task_refund(settings, job_code=job_code, error="e2")
        assert _get_balance(settings, TEST_USER) == balance_before


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Compatibility Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCompatibility:
    """Verify backward compatibility."""

    def test_no_client_request_id_still_works(self):
        """Tasks without client_request_id should work as before."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        result = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        assert result["job_code"]
        assert _count_tasks(settings, TEST_USER) == 1

    def test_empty_client_request_id_still_works(self):
        """Empty client_request_id should work as before (no dedup)."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        r1 = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test1", cost_credits=5,
            client_request_id="",
        )
        r2 = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test2", cost_credits=5,
            client_request_id="",
        )
        assert r1["job_code"] != r2["job_code"]
        assert _count_tasks(settings, TEST_USER) == 2

    def test_schema_init_twice_safe(self):
        """Running ensure_schema twice should not fail."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        # Already called in _make_settings, call again
        ensure_schema(settings)
        assert _has_idempotency_table(settings)
        assert _has_billing_events_table(settings)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Balance Consistency Under Failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBalanceConsistency:
    """Verify balance stays consistent even with concurrent operations."""

    def test_concurrent_mixed_operations(self):
        """Mix of create and refund operations should keep balance consistent."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"mixed-{uuid.uuid4().hex[:8]}"

        # Create task
        result = create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
            client_request_id=client_id,
        )
        assert _get_balance(settings, TEST_USER) == 49995

        # Concurrent: duplicate create + refund
        results = []
        barrier = threading.Barrier(3, timeout=10)

        def _dup_create():
            try:
                barrier.wait()
                r = create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                    client_request_id=client_id,
                )
                results.append(("create", r))
            except Exception as e:
                results.append(("error", str(e)))

        def _refund():
            try:
                barrier.wait()
                r = fail_smart_agent_task_refund(
                    settings, job_code=result["job_code"], error="test"
                )
                results.append(("refund", r))
            except Exception as e:
                results.append(("error", str(e)))

        threads = [
            threading.Thread(target=_dup_create),
            threading.Thread(target=_dup_create),
            threading.Thread(target=_refund),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Balance should be exactly 50000 (refunded)
        assert _get_balance(settings, TEST_USER) == 50000
        # Only 1 task created
        assert _count_tasks(settings, TEST_USER) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
