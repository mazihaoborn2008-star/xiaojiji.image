"""Phase 4 final: Queued task creation integration tests with real SQLite.

Tests create_smart_agent_queued_task_atomic through actual database:
1. Sequential duplicate requests return same task
2. Concurrent duplicate requests create only one task
3. Same ID different payload returns conflict
4. No client_request_id skips idempotency
5. Fault injection at each transaction step
"""
from __future__ import annotations

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
    connect,
    create_smart_agent_queued_task_atomic,
    create_smart_agent_task_atomic,
    ensure_schema,
    fail_smart_agent_task_refund,
)

TEST_USER = "queued-integration-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "queued_integration_cases"


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
        "BALANCE_DB": str(test_root / "queued_test.db"),
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
        "session_secret": "queued-session-secret-32chars!!!!!!",
        "jwt_secret": "queued-jwt-secret-32chars!!!!!!!!!!",
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


def _count_idempotency(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM smart_agent_request_idempotency WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _count_ledger(settings: Settings, user_id: str, reason: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE user_id=? AND reason=?", (user_id, reason)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _make_queued_args(job_code=None, **overrides):
    """Build standard args for create_smart_agent_queued_task_atomic."""
    defaults = {
        "job_code": job_code or f"JOB-{uuid.uuid4().hex[:8]}",
        "user_id": TEST_USER,
        "username": "tester",
        "request_text": "a cute girl in school uniform",
        "cost_credits": 5,
        "plan_json": '{"workflow":"default"}',
        "prompt": "1girl, school uniform, standing",
        "workflow_key": "default",
        "loras_json": "[]",
        "prompt_source": "smart_agent",
        "width": 1024,
        "height": 1536,
    }
    defaults.update(overrides)
    return defaults


class _FailingConnection:
    """Wrapper that injects failures at specific SQL patterns."""

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
                    raise sqlite3.OperationalError(f"Injected: {pattern}")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# A. Sequential Duplicate Requests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedSequentialDedup:
    """Sequential duplicate requests should return same task."""

    def test_same_id_same_payload_returns_existing(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qseq-{uuid.uuid4().hex[:8]}"

        r1 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )
        bal1 = _get_balance(settings, TEST_USER)

        r2 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )
        bal2 = _get_balance(settings, TEST_USER)

        assert r1["job_code"] == r2["job_code"]
        assert r2.get("deduped") is True
        assert bal1 == bal2
        assert _count_tasks(settings, TEST_USER) == 1
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1

    def test_no_client_request_id_creates_separate_tasks(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        r1 = create_smart_agent_queued_task_atomic(settings, **_make_queued_args())
        r2 = create_smart_agent_queued_task_atomic(settings, **_make_queued_args())

        assert r1["job_code"] != r2["job_code"]
        assert _count_tasks(settings, TEST_USER) == 2

    def test_empty_client_request_id_creates_separate_tasks(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        r1 = create_smart_agent_queued_task_atomic(settings, client_request_id="", **_make_queued_args())
        r2 = create_smart_agent_queued_task_atomic(settings, client_request_id="", **_make_queued_args())

        assert r1["job_code"] != r2["job_code"]

    def test_different_user_same_id_no_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, "user-a", 50000)
        _seed_balance(settings, "user-b", 50000)
        cid = f"shared-{uuid.uuid4().hex[:8]}"

        r1 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(user_id="user-a")
        )
        r2 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(user_id="user-b")
        )

        assert r1["job_code"] != r2["job_code"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# B. Concurrent Duplicate Requests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedConcurrentDedup:
    """Concurrent duplicate requests should create only one task."""

    def test_concurrent_same_id_one_task(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconc-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []
        barrier = threading.Barrier(4, timeout=10)

        def _create(idx):
            try:
                barrier.wait()
                r = create_smart_agent_queued_task_atomic(
                    settings, client_request_id=cid, **_make_queued_args()
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) >= 1
        job_codes = {r["job_code"] for r in results}
        assert len(job_codes) == 1
        assert _count_tasks(settings, TEST_USER) == 1
        assert _count_ledger(settings, TEST_USER, SMART_AGENT_CHARGE_REASON) == 1
        assert _get_balance(settings, TEST_USER) == 49995

    def test_concurrent_different_ids_all_succeed(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        results = []
        barrier = threading.Barrier(3, timeout=10)

        def _create(idx):
            barrier.wait()
            r = create_smart_agent_queued_task_atomic(
                settings, client_request_id=f"diff-{idx}-{uuid.uuid4().hex[:4]}",
                **_make_queued_args()
            )
            results.append(r)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) == 3
        job_codes = {r["job_code"] for r in results}
        assert len(job_codes) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# C. Same ID Different Payload Conflict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedConflictDetection:
    """Same ID with different payload should raise conflict."""

    def test_different_prompt_raises_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconf-{uuid.uuid4().hex[:8]}"

        create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )
        bal = _get_balance(settings, TEST_USER)

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_queued_task_atomic(
                settings, client_request_id=cid,
                **_make_queued_args(request_text="completely different request text for testing")
            )

        assert _get_balance(settings, TEST_USER) == bal
        assert _count_tasks(settings, TEST_USER) == 1

    def test_different_workflow_raises_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconfwf-{uuid.uuid4().hex[:8]}"

        create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(workflow_key="default")
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_queued_task_atomic(
                settings, client_request_id=cid, **_make_queued_args(workflow_key="special_wf")
            )

    def test_different_character_raises_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconfch-{uuid.uuid4().hex[:8]}"

        create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(character_key="char_a")
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_queued_task_atomic(
                settings, client_request_id=cid, **_make_queued_args(character_key="char_b")
            )
    def test_different_dimensions_raises_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconfdim-{uuid.uuid4().hex[:8]}"

        create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(width=1024, height=1536)
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_queued_task_atomic(
                settings, client_request_id=cid, **_make_queued_args(width=512, height=512)
            )

    def test_different_prompt_source_raises_conflict(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qconfps-{uuid.uuid4().hex[:8]}"

        create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args(prompt_source="smart_agent")
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_smart_agent_queued_task_atomic(
                settings, client_request_id=cid, **_make_queued_args(prompt_source="user_raw")
            )




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D. Fault Injection - Task Creation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedFaultInjection:
    """Transaction fault injection for queued task creation."""

    def test_balance_failure_rolls_back_everything(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        cid = f"qfbal-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE users SET balance_fen = balance_fen -", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                create_smart_agent_queued_task_atomic(
                    settings, client_request_id=cid, **_make_queued_args()
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count_tasks(settings, TEST_USER) == 0
        assert _count_idempotency(settings, TEST_USER) == 0

    def test_task_insert_failure_rolls_back_everything(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        cid = f"qftask-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("INSERT INTO generation_tasks", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                create_smart_agent_queued_task_atomic(
                    settings, client_request_id=cid, **_make_queued_args()
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count_tasks(settings, TEST_USER) == 0
        assert _count_idempotency(settings, TEST_USER) == 0

    def test_idempotency_complete_failure_rolls_back(self):
        """If _complete_idempotency_record fails, task and charge should roll back."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        initial = _get_balance(settings, TEST_USER)
        cid = f"qfidem-{uuid.uuid4().hex[:8]}"

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE smart_agent_request_idempotency", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                create_smart_agent_queued_task_atomic(
                    settings, client_request_id=cid, **_make_queued_args()
                )
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == initial
        assert _count_tasks(settings, TEST_USER) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E. Fault Injection - Refund
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedRefundFaultInjection:
    """Transaction fault injection for refund."""

    def test_refund_status_update_failure_rolls_back(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job, user_id=TEST_USER,
            username="tester", request_text="test refund", cost_credits=5,
        )
        bal = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE generation_tasks", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(settings, job_code=job, error="test")
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == bal
        conn = connect(settings)
        try:
            row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job,)).fetchone()
            assert row["status"] == "smart_planning"
        finally:
            conn.close()

    def test_refund_balance_failure_rolls_back(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job, user_id=TEST_USER,
            username="tester", request_text="test refund", cost_credits=5,
        )
        bal = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("UPDATE users SET balance_fen=balance_fen+", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(settings, job_code=job, error="test")
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == bal
        conn = connect(settings)
        try:
            row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job,)).fetchone()
            assert row["status"] == "smart_planning"
        finally:
            conn.close()

    def test_refund_ledger_failure_rolls_back(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings, job_code=job, user_id=TEST_USER,
            username="tester", request_text="test refund", cost_credits=5,
        )
        bal = _get_balance(settings, TEST_USER)

        import app.db as db_mod
        orig = db_mod.connect
        def wrapped(s):
            return _FailingConnection(orig(s), [("INSERT INTO balance_ledger", 1)])
        db_mod.connect = wrapped
        try:
            with pytest.raises(sqlite3.OperationalError):
                fail_smart_agent_task_refund(settings, job_code=job, error="test")
        finally:
            db_mod.connect = orig

        assert _get_balance(settings, TEST_USER) == bal
        conn = connect(settings)
        try:
            row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job,)).fetchone()
            assert row["status"] == "smart_planning"
        finally:
            conn.close()




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E. Response Loss Retry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedResponseLossRetry:
    """Simulate HTTP response loss and verify safe retry."""

    def test_retry_after_response_loss_returns_same_task(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qretry-{uuid.uuid4().hex[:8]}"

        r1 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )
        bal = _get_balance(settings, TEST_USER)

        # Simulate response loss: client doesn't use r1, retries same request
        r2 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )

        assert r1["job_code"] == r2["job_code"]
        assert r2.get("deduped") is True
        assert _get_balance(settings, TEST_USER) == bal
        assert _count_tasks(settings, TEST_USER) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# F. Exact Balance Concurrent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedExactBalanceConcurrent:
    """Concurrent requests with exact-balance should not create negative balance."""

    def test_exact_balance_concurrent_no_negative(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 5)  # Exactly enough for one
        cid = f"qexact-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []
        barrier = threading.Barrier(2, timeout=10)

        def _create():
            try:
                barrier.wait()
                r = create_smart_agent_queued_task_atomic(
                    settings, client_request_id=cid, **_make_queued_args()
                )
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_create) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) >= 1
        bal = _get_balance(settings, TEST_USER)
        assert bal >= 0, f"Balance went negative: {bal}"
        assert _count_tasks(settings, TEST_USER) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# G. Task Completion Replay
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQueuedTaskCompletionReplay:
    """Replay after task completion should return original task."""

    def test_replay_after_completion_returns_original(self):
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        cid = f"qreplay-{uuid.uuid4().hex[:8]}"

        r1 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )
        job = r1["job_code"]

        # Simulate task completion
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE generation_tasks SET status='done', finished_at=? WHERE job_code=?",
                (1000, job),
            )
            conn.commit()
        finally:
            conn.close()

        bal = _get_balance(settings, TEST_USER)

        # Replay same request
        r2 = create_smart_agent_queued_task_atomic(
            settings, client_request_id=cid, **_make_queued_args()
        )

        assert r2["job_code"] == job
        assert _get_balance(settings, TEST_USER) == bal
        assert _count_tasks(settings, TEST_USER) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
