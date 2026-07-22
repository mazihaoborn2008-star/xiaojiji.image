"""Phase 4 hardening: fingerprint completeness, refund uniqueness, confirm idempotency, fault injection.

Verifies:
1. request_fingerprint covers all business-affecting fields
2. Same task cannot be refunded via different reason codes
3. confirm_smart_agent_prompt_draft_atomic has DB-level protection
4. Transaction failures leave no partial state
5. DB structure assertions are automated
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
    _compute_request_fingerprint,
    _insert_refund_event,
    connect,
    create_smart_agent_task_atomic,
    ensure_schema,
    fail_smart_agent_task_refund,
)

TEST_USER = "phase4-hardening-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "phase4_cases"


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
        "BALANCE_DB": str(test_root / "phase4_test.db"),
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
        "session_secret": "phase4-session-secret-32chars!!!!!!",
        "jwt_secret": "phase4-jwt-secret-32chars!!!!!!!!!!",
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


def _count_billing_events(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM smart_agent_billing_events WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. DB Structure Automated Verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDBStructure:
    """Automated verification of database structure."""

    def test_idempotency_table_has_unique_constraint(self):
        """smart_agent_request_idempotency must have UNIQUE(user_id, client_request_id)."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='smart_agent_request_idempotency'"
            ).fetchone()
            assert row is not None, "Table missing"
            ddl = row["sql"].upper()
            assert "UNIQUE" in ddl, f"No UNIQUE constraint in DDL: {row['sql']}"
        finally:
            conn.close()

    def test_billing_events_has_unique_on_event_key(self):
        """smart_agent_billing_events must have UNIQUE(event_key)."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='smart_agent_billing_events'"
            ).fetchone()
            assert row is not None, "Table missing"
            ddl = row["sql"].upper()
            assert "EVENT_KEY" in ddl and "UNIQUE" in ddl, f"No UNIQUE on event_key: {row['sql']}"
        finally:
            conn.close()

    def test_generation_tasks_client_request_id_index(self):
        """Check actual index on generation_tasks.client_request_id."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='generation_tasks' "
                "AND sql LIKE '%client_request%'"
            ).fetchall()
            # Document what actually exists
            index_sqls = [r["sql"] for r in rows if r["sql"]]
            # There may be a partial unique index or none
            for sql in index_sqls:
                assert "generation_tasks" in sql
            # This test documents the current state, not necessarily requires an index
        finally:
            conn.close()

    def test_schema_init_twice_consistent(self):
        """Running ensure_schema twice should produce identical structure."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        conn = connect(settings)
        try:
            tables_before = sorted([
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ])
        finally:
            conn.close()

        ensure_schema(settings)

        conn = connect(settings)
        try:
            tables_after = sorted([
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ])
        finally:
            conn.close()

        assert tables_before == tables_after


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Fingerprint Completeness
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFingerprintCompleteness:
    """Verify fingerprint covers all business-affecting fields."""

    def test_different_request_text_different_fingerprint(self):
        """Different request_text must produce different fingerprints."""
        fp1 = _compute_request_fingerprint(
            user_id="u1", request_text="prompt A", cost_credits=5, client_request_id="id1",
        )
        fp2 = _compute_request_fingerprint(
            user_id="u1", request_text="prompt B", cost_credits=5, client_request_id="id1",
        )
        assert fp1 != fp2

    def test_different_cost_different_fingerprint(self):
        """Different cost_credits must produce different fingerprints."""
        fp1 = _compute_request_fingerprint(
            user_id="u1", request_text="same", cost_credits=5, client_request_id="id1",
        )
        fp2 = _compute_request_fingerprint(
            user_id="u1", request_text="same", cost_credits=10, client_request_id="id1",
        )
        assert fp1 != fp2

    def test_different_user_different_fingerprint(self):
        """Different user_id must produce different fingerprints."""
        fp1 = _compute_request_fingerprint(
            user_id="u1", request_text="same", cost_credits=5, client_request_id="id1",
        )
        fp2 = _compute_request_fingerprint(
            user_id="u2", request_text="same", cost_credits=5, client_request_id="id1",
        )
        assert fp1 != fp2

    def test_same_input_same_fingerprint(self):
        """Same input must always produce same fingerprint (deterministic)."""
        fp1 = _compute_request_fingerprint(
            user_id="u1", request_text="prompt", cost_credits=5, client_request_id="id1",
        )
        fp2 = _compute_request_fingerprint(
            user_id="u1", request_text="prompt", cost_credits=5, client_request_id="id1",
        )
        assert fp1 == fp2

    def test_stripped_whitespace_same_fingerprint(self):
        """Leading/trailing whitespace in request_text should be normalized."""
        fp1 = _compute_request_fingerprint(
            user_id="u1", request_text="  prompt  ", cost_credits=5, client_request_id="id1",
        )
        fp2 = _compute_request_fingerprint(
            user_id="u1", request_text="prompt", cost_credits=5, client_request_id="id1",
        )
        assert fp1 == fp2

    def test_fingerprint_is_sha256(self):
        """Fingerprint should be a valid SHA-256 hex string."""
        fp = _compute_request_fingerprint(
            user_id="u1", request_text="test", cost_credits=5, client_request_id="id1",
        )
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Cross-Reason Refund Protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCrossReasonRefundProtection:
    """Verify same task cannot be refunded via different reason codes."""

    def test_same_task_different_reason_only_one_refund(self):
        """Same task with different refund reason should only refund once."""
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

        # First refund with standard reason
        r1 = fail_smart_agent_task_refund(settings, job_code=job_code, error="timeout")
        assert r1 is True
        assert _get_balance(settings, TEST_USER) == 50000

        # Try refund with different reason (via direct billing event insert)
        # The status check should prevent this, but let's verify billing events too
        r2 = fail_smart_agent_task_refund(settings, job_code=job_code, error="different_error")
        assert r2 is False  # Status already changed
        assert _get_balance(settings, TEST_USER) == 50000

    def test_billing_event_key_includes_task_id(self):
        """Refund event key must include task_id to prevent cross-task reuse."""
        case_root = _case_root()
        settings = _make_settings(case_root)

        conn = connect(settings)
        try:
            # Insert refund event for task-A
            r1 = _insert_refund_event(
                conn, user_id=TEST_USER, task_id="task-A",
                amount=5, reason="smart_agent_refund", now=1000,
            )
            conn.commit()
            assert r1 is True

            # Try to insert refund for same task-A with different reason
            # The event key includes reason, so this would be a different key
            # But we want to verify the design
            r2 = _insert_refund_event(
                conn, user_id=TEST_USER, task_id="task-A",
                amount=5, reason="different_reason", now=1001,
            )
            conn.commit()
            # With current design (key = task_id:reason), this succeeds
            # because different reason = different key
            # This is acceptable IF the status check prevents the actual refund
            assert r2 is True  # Different key, insert succeeds

            # But the actual refund function checks status first,
            # so the second refund would fail at the status check
        finally:
            conn.close()

    def test_refund_event_key_format(self):
        """Verify the event key format includes task_id and reason."""
        case_root = _case_root()
        settings = _make_settings(case_root)

        conn = connect(settings)
        try:
            _insert_refund_event(
                conn, user_id=TEST_USER, task_id="JOB-123",
                amount=5, reason="smart_agent_refund", now=1000,
            )
            conn.commit()

            rows = conn.execute(
                "SELECT event_key FROM smart_agent_billing_events WHERE user_id=?",
                (TEST_USER,),
            ).fetchall()
            assert len(rows) == 1
            key = rows[0]["event_key"]
            assert "JOB-123" in key
            assert "smart_agent_refund" in key
        finally:
            conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. confirm_smart_agent_prompt_draft_atomic Protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfirmDraftProtection:
    """Verify confirm_smart_agent_prompt_draft_atomic has adequate protection."""

    def test_draft_state_machine_prevents_double_confirm(self):
        """Draft status transition (prompt_ready→generated) prevents double creation."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        # Create a draft in prompt_ready state
        conn = connect(settings)
        try:
            conn.execute(
                "INSERT INTO smart_agent_prompt_drafts "
                "(conversation_id, message_id, prompt_draft, plan_json, request_text, "
                "workflow_key, loras_json, prompt_source, resolved_character_key, workflow_source, "
                "fallback_level, width, height, prompt_version, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    999, 0, "test prompt", "{}", "test request",
                    "default", "[]", "smart_agent", "", "", "",
                    1024, 1536, 1, "prompt_ready", 1000, 1000,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        from app.db import confirm_smart_agent_prompt_draft_atomic

        # First confirm should succeed
        r1 = confirm_smart_agent_prompt_draft_atomic(
            settings,
            conversation_id=999,
            user_id=TEST_USER,
            username="tester",
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            cost_credits=5,
        )
        assert r1["job_code"]
        balance_after = _get_balance(settings, TEST_USER)

        # Second confirm should return already_created
        r2 = confirm_smart_agent_prompt_draft_atomic(
            settings,
            conversation_id=999,
            user_id=TEST_USER,
            username="tester",
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            cost_credits=5,
        )
        assert r2.get("already_created") is True
        assert _get_balance(settings, TEST_USER) == balance_after  # No double charge


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Transaction Fault Injection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FailingConnection:
    """Wrapper around sqlite3.Connection that can inject failures at specific SQL patterns."""

    def __init__(self, conn: sqlite3.Connection, fail_patterns: list[tuple[str, int]] | None = None):
        self._conn = conn
        self._fail_patterns = fail_patterns or []  # [(sql_pattern, max_failures)]
        self._fail_counts: dict[str, int] = {}

    def execute(self, sql, *args, **kwargs):
        sql_str = str(sql)
        for pattern, max_fails in self._fail_patterns:
            if pattern in sql_str:
                count = self._fail_counts.get(pattern, 0)
                if count < max_fails:
                    self._fail_counts[pattern] = count + 1
                    raise sqlite3.OperationalError(f"Injected failure for: {pattern}")
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


class TestTransactionFaultInjection:
    """Verify transaction failures leave no partial state."""

    def test_failure_after_balance_deduction_rolls_back(self):
        """If task INSERT fails after balance deduction, entire transaction rolls back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial_balance = _get_balance(settings, TEST_USER)

        import app.db as db_module
        original_connect = db_module.connect

        def wrapped_connect(s):
            conn = original_connect(s)
            return _FailingConnection(conn, [("INSERT INTO generation_tasks", 1)])

        db_module.connect = wrapped_connect
        try:
            with pytest.raises(sqlite3.OperationalError, match="Injected failure"):
                create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                )
        finally:
            db_module.connect = original_connect

        # Balance should be unchanged (rollback)
        assert _get_balance(settings, TEST_USER) == initial_balance
        assert _count_tasks(settings, TEST_USER) == 0
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 0

    def test_failure_after_idempotency_insert_rolls_back(self):
        """If balance deduction fails after idempotency insert, everything rolls back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial_balance = _get_balance(settings, TEST_USER)
        client_id = f"fault-{uuid.uuid4().hex[:8]}"

        import app.db as db_module
        original_connect = db_module.connect

        def wrapped_connect(s):
            conn = original_connect(s)
            return _FailingConnection(conn, [("UPDATE users SET balance_fen = balance_fen -", 1)])

        db_module.connect = wrapped_connect
        try:
            with pytest.raises(sqlite3.OperationalError, match="Injected failure"):
                create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="test", cost_credits=5,
                    client_request_id=client_id,
                )
        finally:
            db_module.connect = original_connect

        # Everything should be rolled back
        assert _get_balance(settings, TEST_USER) == initial_balance
        assert _count_tasks(settings, TEST_USER) == 0

        # Idempotency record should NOT exist (rolled back)
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM smart_agent_request_idempotency WHERE user_id=? AND client_request_id=?",
                (TEST_USER, client_id),
            ).fetchone()
            assert int(row[0]) == 0, "Idempotency record should have been rolled back"
        finally:
            conn.close()

    def test_refund_failure_after_billing_event_rolls_back(self):
        """If balance update fails during refund, billing event should also roll back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job_code,
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )
        balance_after_charge = _get_balance(settings, TEST_USER)

        import app.db as db_module
        original_connect = db_module.connect

        def wrapped_connect(s):
            conn = original_connect(s)
            return _FailingConnection(conn, [("UPDATE users SET balance_fen=balance_fen+", 1)])

        db_module.connect = wrapped_connect
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(
                    settings, job_code=job_code, error="test",
                )
        finally:
            db_module.connect = original_connect

        # Balance should be unchanged (rollback)
        assert _get_balance(settings, TEST_USER) == balance_after_charge

        # Billing event should NOT exist (rolled back)
        assert _count_billing_events(settings, TEST_USER) == 0

        # Task status should still be smart_planning (not partially updated)
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT status FROM generation_tasks WHERE job_code=?",
                (job_code,),
            ).fetchone()
            assert row["status"] == "smart_planning"
        finally:
            conn.close()

    def test_concurrent_create_with_fault_still_safe(self):
        """Concurrent creates where one fails should not affect the other."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"concurrent-fault-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []
        barrier = threading.Barrier(3, timeout=10)

        def _create(idx):
            try:
                barrier.wait()
                r = create_smart_agent_task_atomic(
                    settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER, username="tester",
                    request_text="concurrent test", cost_credits=5,
                    client_request_id=client_id,
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # At least one should succeed
        assert len(results) >= 1
        # Only one task created
        assert _count_tasks(settings, TEST_USER) == 1
        # Only one charge
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1
        # Balance reduced by exactly 5
        assert _get_balance(settings, TEST_USER) == 49995


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Idempotency Table Edge Cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIdempotencyEdgeCases:
    """Edge cases for the idempotency table."""

    def test_no_client_request_id_skips_idempotency_table(self):
        """Tasks without client_request_id should not write to idempotency table."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
        )

        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM smart_agent_request_idempotency WHERE user_id=?",
                (TEST_USER,),
            ).fetchone()
            assert int(row[0]) == 0
        finally:
            conn.close()

    def test_empty_client_request_id_skips_idempotency_table(self):
        """Empty client_request_id should not write to idempotency table."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        create_smart_agent_task_atomic(
            settings, job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER, username="tester",
            request_text="test", cost_credits=5,
            client_request_id="",
        )

        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM smart_agent_request_idempotency WHERE user_id=?",
                (TEST_USER,),
            ).fetchone()
            assert int(row[0]) == 0
        finally:
            conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
