"""Cancellation Under Load Tests.

Tests for resilient cancellation under burst load:
A. Fast cancel 10 tasks within rate limit window
B. Window with existing old cancels then cancel 10
C. Fast repeated cancel (idempotent, no double refund)
D. Concurrent cancel (two threads, no double refund)
E. Cancel response contains authoritative balance
F. Both translating and queued states cancel correctly
G. Processing and done return structured 409
H. Frontend static contract checks
"""
from __future__ import annotations

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
    connect,
    ensure_schema,
    create_fast_translation_task_atomic,
    cancel_task_atomic,
    make_job_code,
)

TEST_USER_A = "test-user-cancel-load"
TEST_USER_B = "test-user-cancel-load-b"


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _make_settings(tmp_path: Path, **overrides) -> Settings:
    db_path = tmp_path / "test.db"
    output = tmp_path / "output"
    mock_output = tmp_path / "mock_output"
    input_images = tmp_path / "input_images"
    for d in (output, mock_output, input_images):
        d.mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(db_path),
        "BOT_OUTPUT_DIR": str(output),
        "mock_output_dir": str(mock_output),
        "INPUT_IMAGE_DIR": str(input_images),
        "BOT_DIR": str(tmp_path),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER_A,
        "dev_username": "CancelLoadTester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "price_fen_per_image": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_dummy",
        "deepseek_base_url": "http://127.0.0.1:9",
        "deepseek_model": "test-mock",
        "session_secret": "test-session-secret-for-cancel-load-32c!!",
        "jwt_secret": "test-jwt-secret-for-cancel-load-testing",
        "agent_enabled": False,
        "max_active_tasks_per_user": 10,
        "generation_submit_user_limit": 20,
        "generation_submit_window_seconds": 60,
        "cancel_submit_user_limit": 60,
        "cancel_submit_window_seconds": 60,
        "max_queue_size": 50,
        "owner_free_generation": False,
        "owner_user_id": "owner-123",
    }
    data.update(overrides)
    return Settings(**data)


def _seed_user(s: Settings, user_id: str, balance: int = 100):
    conn = connect(s)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (user_id, balance))
        conn.commit()
    finally:
        conn.close()


def _get_balance(s: Settings, user_id: str) -> int:
    conn = connect(s)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _get_task_status(s: Settings, job_code: str) -> str:
    conn = connect(s)
    try:
        row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        return str(row["status"]) if row else ""
    finally:
        conn.close()


def _count_ledger(s: Settings, user_id: str, reason: str) -> int:
    conn = connect(s)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE user_id=? AND reason=?",
            (user_id, reason),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _create_task(s: Settings, user_id: str, prompt: str = "test prompt") -> dict:
    """Create a fast translation task in translating state."""
    return create_fast_translation_task_atomic(
        s,
        job_code=make_job_code(),
        user_id=user_id,
        username="tester",
        original_prompt=prompt,
        translation_mode="fast",
        style_key="style_a",
        lora_weight=1.0,
        width=1024,
        height=1536,
        mode="txt2img",
        input_image_path=None,
        denoise=0.5,
        control_type="depth",
        control_character="prompt",
        auto_tagger=False,
    )


def _set_task_status(s: Settings, job_code: str, status: str):
    """Directly set a task's status for testing state transitions."""
    conn = connect(s)
    try:
        conn.execute("UPDATE generation_tasks SET status=? WHERE job_code=?", (status, job_code))
        conn.commit()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
# A. Fast cancel 10 tasks
# ────────────────────────────────────────────────────────────

class TestBurstCancelTenTasks:
    """Cancel 10 tasks rapidly within the rate limit window."""

    def test_cancel_ten_tasks_all_succeed(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 1000)

        job_codes = []
        for i in range(10):
            r = _create_task(s, TEST_USER_A, f"prompt {i}")
            job_codes.append(r["job_code"])

        # All should be translating
        for jc in job_codes:
            assert _get_task_status(s, jc) == "translating"

        # Cancel all 10 rapidly
        results = []
        for jc in job_codes:
            r = cancel_task_atomic(s, TEST_USER_A, jc)
            results.append(r)

        # All should succeed
        assert len(results) == 10
        for r in results:
            assert r["status"] == "cancelled_refunded"
            assert r["already_cancelled"] is False
            assert r["refunded_fen"] > 0

        # All tasks cancelled
        for jc in job_codes:
            assert _get_task_status(s, jc) == "cancelled_refunded"

        # Balance fully restored
        assert _get_balance(s, TEST_USER_A) == 1000

    def test_cancel_ten_with_history_cancels(self, tmp_path):
        """5 old cancels + 10 new cancels within 60s window — all succeed."""
        s = _make_settings(tmp_path, cancel_submit_user_limit=60)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 2000)

        # Create and cancel 5 old tasks
        for i in range(5):
            r = _create_task(s, TEST_USER_A, f"old prompt {i}")
            cancel_task_atomic(s, TEST_USER_A, r["job_code"])

        # Now create and cancel 10 more
        new_jobs = []
        for i in range(10):
            r = _create_task(s, TEST_USER_A, f"new prompt {i}")
            new_jobs.append(r["job_code"])

        results = []
        for jc in new_jobs:
            r = cancel_task_atomic(s, TEST_USER_A, jc)
            results.append(r)

        assert len(results) == 10
        for r in results:
            assert r["already_cancelled"] is False
            assert r["refunded_fen"] > 0

        assert _get_balance(s, TEST_USER_A) == 2000


# ────────────────────────────────────────────────────────────
# C. Fast repeated cancel (idempotent)
# ────────────────────────────────────────────────────────────

class TestIdempotentCancel:
    """Repeated cancel on same task returns already_cancelled."""

    def test_repeated_cancel_five_times(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        jc = r["job_code"]
        charged = r["charged_fen"]

        # First cancel
        r1 = cancel_task_atomic(s, TEST_USER_A, jc)
        assert r1["refunded_fen"] == charged
        assert r1["already_cancelled"] is False
        balance_after = r1["balance_fen"]
        assert balance_after == 100

        # Next 4 cancels — idempotent
        for _ in range(4):
            r2 = cancel_task_atomic(s, TEST_USER_A, jc)
            assert r2["refunded_fen"] == 0
            assert r2["already_cancelled"] is True
            assert r2["balance_fen"] == balance_after

        # Only one refund ledger entry
        assert _count_ledger(s, TEST_USER_A, "generate_cancel_refund") == 1


# ────────────────────────────────────────────────────────────
# D. Concurrent cancel
# ────────────────────────────────────────────────────────────

class TestConcurrentCancel:
    """Two threads cancel the same task simultaneously."""

    def test_concurrent_cancel_no_double_refund(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        jc = r["job_code"]
        charged = r["charged_fen"]

        results = []
        errors = []

        def try_cancel():
            try:
                result = cancel_task_atomic(s, TEST_USER_A, jc)
                results.append(result)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=try_cancel)
        t2 = threading.Thread(target=try_cancel)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both succeed
        assert len(results) == 2
        assert len(errors) == 0

        refunded = [r["refunded_fen"] for r in results]
        already = [r["already_cancelled"] for r in results]

        # Exactly one got the refund
        assert sum(refunded) == charged
        assert already.count(True) == 1
        assert already.count(False) == 1

        # Balance restored exactly once
        assert _get_balance(s, TEST_USER_A) == 100
        assert _count_ledger(s, TEST_USER_A, "generate_cancel_refund") == 1


# ────────────────────────────────────────────────────────────
# E. Cancel response balance
# ────────────────────────────────────────────────────────────

class TestCancelResponseBalance:
    """Cancel response balance_fen matches database exactly."""

    def test_balance_matches_database(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 500)

        r = _create_task(s, TEST_USER_A)
        jc = r["job_code"]

        result = cancel_task_atomic(s, TEST_USER_A, jc)
        db_balance = _get_balance(s, TEST_USER_A)

        assert result["balance_fen"] == db_balance
        assert result["balance_fen"] == 500

    def test_response_has_all_fields(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 200)

        r = _create_task(s, TEST_USER_A)
        jc = r["job_code"]

        result = cancel_task_atomic(s, TEST_USER_A, jc)

        assert "job_code" in result
        assert "status" in result
        assert "refunded_fen" in result
        assert "balance_fen" in result
        assert "already_cancelled" in result
        assert result["job_code"] == jc
        assert result["status"] == "cancelled_refunded"


# ────────────────────────────────────────────────────────────
# F. translating and queued cancel
# ────────────────────────────────────────────────────────────

class TestCancelByStatus:
    """Cancel works for both translating and queued states."""

    def test_cancel_translating(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        assert r["status"] == "translating"

        result = cancel_task_atomic(s, TEST_USER_A, r["job_code"])
        assert result["status"] == "cancelled_refunded"
        assert result["refunded_fen"] == r["charged_fen"]
        assert _get_balance(s, TEST_USER_A) == 100

    def test_cancel_queued(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        _set_task_status(s, r["job_code"], "queued")

        result = cancel_task_atomic(s, TEST_USER_A, r["job_code"])
        assert result["status"] == "cancelled_refunded"
        assert result["refunded_fen"] == r["charged_fen"]
        assert _get_balance(s, TEST_USER_A) == 100


# ────────────────────────────────────────────────────────────
# G. Processing and done return 409
# ────────────────────────────────────────────────────────────

class TestCancelProcessingDone:
    """Cancel on processing/done states returns structured RuntimeError."""

    def test_cancel_processing_raises(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        _set_task_status(s, r["job_code"], "processing")

        with pytest.raises(RuntimeError, match="正在生成中"):
            cancel_task_atomic(s, TEST_USER_A, r["job_code"])

        # Balance unchanged
        assert _get_balance(s, TEST_USER_A) == 100 - r["charged_fen"]

    def test_cancel_done_raises(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        r = _create_task(s, TEST_USER_A)
        _set_task_status(s, r["job_code"], "done")

        with pytest.raises(RuntimeError, match="已完成"):
            cancel_task_atomic(s, TEST_USER_A, r["job_code"])

        # Balance unchanged
        assert _get_balance(s, TEST_USER_A) == 100 - r["charged_fen"]


# ────────────────────────────────────────────────────────────
# H. Frontend static contract
# ────────────────────────────────────────────────────────────

class TestFrontendStaticContract:
    """Verify frontend JS contains required cancel patterns."""

    def _read_app_js(self) -> str:
        js_path = _ROOT / "app" / "static" / "app.js"
        return js_path.read_text(encoding="utf-8")

    def test_cancel_in_flight_jobs_set_exists(self):
        src = self._read_app_js()
        assert "cancelInFlightJobs" in src
        assert "new Set()" in src

    def test_unified_cancel_helper_exists(self):
        src = self._read_app_js()
        assert "async function cancelTaskAndRefresh" in src

    def test_cancel_uses_balance_fen_from_response(self):
        src = self._read_app_js()
        assert "res.balance_fen" in src or "res && res.balance_fen" in src

    def test_no_alert_in_cancel_flow(self):
        """No raw alert(e.message) in cancel handler areas."""
        src = self._read_app_js()
        # The unified helper uses setMessage, not alert
        # Check that alert is not used in the cancelTaskAndRefresh function
        helper_start = src.find("async function cancelTaskAndRefresh")
        if helper_start == -1:
            pytest.fail("cancelTaskAndRefresh not found")
        # Find the end of the function (next top-level function)
        helper_end = src.find("\nfunction ", helper_start + 1)
        if helper_end == -1:
            helper_end = len(src)
        helper_body = src[helper_start:helper_end]
        assert "alert(" not in helper_body, "cancelTaskAndRefresh must not use alert()"

    def test_refresh_uses_promise_all_settled(self):
        src = self._read_app_js()
        assert "Promise.allSettled" in src

    def test_cancel_button_disabled_on_click(self):
        """cancelTaskAndRefresh disables the button."""
        src = self._read_app_js()
        helper_start = src.find("async function cancelTaskAndRefresh")
        helper_end = src.find("\nfunction ", helper_start + 1)
        helper_body = src[helper_start:helper_end]
        assert "disabled = true" in helper_body

    def test_no_duplicate_cancel_request(self):
        """cancelInFlightJobs prevents duplicate requests."""
        src = self._read_app_js()
        helper_start = src.find("async function cancelTaskAndRefresh")
        helper_end = src.find("\nfunction ", helper_start + 1)
        helper_body = src[helper_start:helper_end]
        assert "cancelInFlightJobs.has(jobCode)" in helper_body
        assert "cancelInFlightJobs.delete(jobCode)" in helper_body

    def test_cancel_handlers_use_unified_helper(self):
        """All cancel button onclick handlers use cancelTaskAndRefresh."""
        src = self._read_app_js()
        # Count occurrences of cancelTaskAndRefresh in onclick handlers
        assert "cancelTaskAndRefresh(task.job_code" in src

    def test_api_error_has_url_property(self):
        """api() attaches url property to errors."""
        src = self._read_app_js()
        assert "error.url = url" in src


# ────────────────────────────────────────────────────────────
# HTTP endpoint tests
# ────────────────────────────────────────────────────────────

class TestHTTPCancelEndpoint:
    """Test the HTTP cancel endpoint via TestClient."""

    def test_http_cancel_returns_structured_response(self, tmp_path):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        _seed_user(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            # Create a task directly via DB
            r = _create_task(settings, TEST_USER_A)
            jc = r["job_code"]

            resp = client.post(f"/api/tasks/{jc}/cancel", headers={"X-CSRF-Token": "test"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["job_code"] == jc
            assert data["status"] == "cancelled_refunded"
            assert data["refunded_fen"] > 0
            assert data["balance_fen"] == 50000
            assert data["already_cancelled"] is False
        finally:
            app.dependency_overrides.clear()

    def test_http_cancel_idempotent(self, tmp_path):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        _seed_user(settings, TEST_USER_A, 500)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = _create_task(settings, TEST_USER_A)
            jc = r["job_code"]

            # First cancel
            resp1 = client.post(f"/api/tasks/{jc}/cancel", headers={"X-CSRF-Token": "test"})
            assert resp1.status_code == 200
            assert resp1.json()["already_cancelled"] is False

            # Second cancel — idempotent
            resp2 = client.post(f"/api/tasks/{jc}/cancel", headers={"X-CSRF-Token": "test"})
            assert resp2.status_code == 200
            assert resp2.json()["already_cancelled"] is True
            assert resp2.json()["refunded_fen"] == 0
            assert resp2.json()["balance_fen"] == 500
        finally:
            app.dependency_overrides.clear()

    def test_http_cancel_processing_returns_409(self, tmp_path):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        _seed_user(settings, TEST_USER_A, 500)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = _create_task(settings, TEST_USER_A)
            _set_task_status(settings, r["job_code"], "processing")

            resp = client.post(f"/api/tasks/{r['job_code']}/cancel", headers={"X-CSRF-Token": "test"})
            assert resp.status_code == 409
        finally:
            app.dependency_overrides.clear()

    def test_http_cancel_done_returns_409(self, tmp_path):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        ensure_schema(settings)
        _seed_user(settings, TEST_USER_A, 500)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = _create_task(settings, TEST_USER_A)
            _set_task_status(settings, r["job_code"], "done")

            resp = client.post(f"/api/tasks/{r['job_code']}/cancel", headers={"X-CSRF-Token": "test"})
            assert resp.status_code == 409
        finally:
            app.dependency_overrides.clear()
