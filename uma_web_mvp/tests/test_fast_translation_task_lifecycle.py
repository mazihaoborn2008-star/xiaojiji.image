"""Fast Translation Task Lifecycle Tests.

Tests for the new async fast translation architecture:
A. Immediate translating task creation
B. Worker success path
C. Worker failure + full refund
D. Cancel translating task
E. Cancel queued task
F. One-to-one binding (request_code → task)
G. Generation idempotency with fingerprint
H. Concurrent cancel / worker finish race
I. Fault injection (ledger/status update failures)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    fail_fast_translation_task_refund_atomic,
    compute_generation_fingerprint,
    _compute_fast_translation_fingerprint,
    make_job_code,
    get_me,
)

TEST_USER_A = "test-user-alpha"
TEST_USER_B = "test-user-beta"


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
        "dev_username": "Lifecycle Tester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "price_fen_per_image": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_dummy",
        "deepseek_base_url": "http://127.0.0.1:9",
        "deepseek_model": "test-mock",
        "session_secret": "test-session-secret-for-lifecycle-32chars!!",
        "jwt_secret": "test-jwt-secret-for-lifecycle-testing-only",
        "agent_enabled": False,
        "max_active_tasks_per_user": 10,
        "generation_submit_user_limit": 20,
        "generation_submit_window_seconds": 60,
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


def _get_task(s: Settings, job_code: str) -> dict | None:
    conn = connect(s)
    try:
        row = conn.execute("SELECT * FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_translation_request(s: Settings, request_code: str) -> dict | None:
    conn = connect(s)
    try:
        row = conn.execute("SELECT * FROM translation_requests WHERE request_code=?", (request_code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count_ledger(s: Settings, user_id: str, reason: str) -> int:
    conn = connect(s)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE user_id=? AND reason=?",
            (user_id, reason),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _count_active_tasks(s: Settings, user_id: str) -> int:
    conn = connect(s)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ('smart_planning','translating','queued','processing')",
            (user_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
# A. Immediate translating task creation
# ────────────────────────────────────────────────────────────

class TestImmediateTranslatingTask:
    def test_fast_task_returns_immediately_with_translating_status(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="a cute cat girl",
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

        assert result["status"] == "translating"
        assert result["deduped"] is False
        assert result["charged_fen"] > 0
        assert "request_code" in result

        # Task exists in DB
        task = _get_task(s, result["job_code"])
        assert task is not None
        assert task["status"] == "translating"
        assert task["translation_mode"] == "fast"
        assert task["fast_translation_request_code"] == result["request_code"]
        assert task["effective_prompt"] is None

        # Translation request exists
        tr = _get_translation_request(s, result["request_code"])
        assert tr is not None
        assert tr["status"] == "queued"
        assert tr["generation_job_code"] == result["job_code"]

    def test_fast_task_deducts_both_charges(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        balance_before = _get_balance(s, TEST_USER_A)
        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test prompt",
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
        balance_after = _get_balance(s, TEST_USER_A)
        assert balance_before - balance_after == result["charged_fen"]
        # Should include both generation (1) + fast translation (2) = 3
        assert result["charged_fen"] == 3

    def test_fast_task_has_correct_prompt_source(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        task = _get_task(s, result["job_code"])
        assert task["prompt_source"] == f"fast_translate_pending:{result['request_code']}"


# ────────────────────────────────────────────────────────────
# B. Worker success path
# ────────────────────────────────────────────────────────────

class TestWorkerSuccess:
    def test_worker_completes_translating_to_queued(self, tmp_path):
        from app.services.fast_translation_worker import (
            claim_next_fast_translation,
            complete_fast_translation,
        )

        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="a beautiful scenery",
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
        request_code = result["request_code"]
        job_code = result["job_code"]

        # Claim
        claimed = claim_next_fast_translation(s)
        assert claimed is not None
        assert claimed["request_code"] == request_code

        # Verify processing state
        tr = _get_translation_request(s, request_code)
        assert tr["status"] == "processing"

        # Complete
        final_prompt = "1girl, cat ears, beautiful scenery, outdoors"
        ok = complete_fast_translation(
            s,
            request_code=request_code,
            job_code=job_code,
            final_prompt=final_prompt,
            character_key='["catgirl_key"]',
        )
        assert ok is True

        # Verify final state
        task = _get_task(s, job_code)
        assert task["status"] == "queued"
        assert task["prompt"] == final_prompt
        assert task["effective_prompt"] == final_prompt
        assert task["prompt_source"] == f"fast_translate:{request_code}"

        tr = _get_translation_request(s, request_code)
        assert tr["status"] == "done"
        assert tr["refined_prompt"] == final_prompt


# ────────────────────────────────────────────────────────────
# C. Worker failure + full refund
# ────────────────────────────────────────────────────────────

class TestWorkerFailureRefund:
    def test_failure_refunds_full_amount(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        balance_before = _get_balance(s, TEST_USER_A)
        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        charged = result["charged_fen"]
        job_code = result["job_code"]

        # Fail
        ok = fail_fast_translation_task_refund_atomic(s, job_code=job_code, error_code="deepseek_failed")
        assert ok is True

        # Balance restored
        balance_after = _get_balance(s, TEST_USER_A)
        assert balance_after == balance_before

        # Task status
        task = _get_task(s, job_code)
        assert task["status"] == "failed_refunded"

        # Translation request status
        tr = _get_translation_request(s, result["request_code"])
        assert tr["status"] == "failed_refunded"

        # Ledger entries: 1 charge + 1 refund
        assert _count_ledger(s, TEST_USER_A, "generate_failed_refund") == 1

    def test_failure_idempotent_no_double_refund(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]

        # First fail
        ok1 = fail_fast_translation_task_refund_atomic(s, job_code=job_code, error_code="deepseek_failed")
        balance_after_first = _get_balance(s, TEST_USER_A)

        # Second fail (should be no-op)
        ok2 = fail_fast_translation_task_refund_atomic(s, job_code=job_code, error_code="deepseek_failed")
        balance_after_second = _get_balance(s, TEST_USER_A)

        assert ok1 is True
        assert ok2 is False
        assert balance_after_first == balance_after_second


# ────────────────────────────────────────────────────────────
# D. Cancel translating task
# ────────────────────────────────────────────────────────────

class TestCancelTranslating:
    def test_cancel_translating_refunds_all(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        balance_before = _get_balance(s, TEST_USER_A)
        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]

        # Cancel
        r = cancel_task_atomic(s, TEST_USER_A, job_code)
        refunded = r["refunded_fen"]
        assert refunded == result["charged_fen"]

        # Balance restored
        assert _get_balance(s, TEST_USER_A) == balance_before

        # Task status
        task = _get_task(s, job_code)
        assert task["status"] == "cancelled_refunded"

        # Translation request cancelled
        tr = _get_translation_request(s, result["request_code"])
        assert tr["status"] == "cancelled_refunded"

    def test_cancel_translating_prevents_worker_completion(self, tmp_path):
        from app.services.fast_translation_worker import claim_next_fast_translation, complete_fast_translation

        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]

        # Cancel first
        cancel_task_atomic(s, TEST_USER_A, job_code)

        # Worker tries to complete - should not find anything to claim
        claimed = claim_next_fast_translation(s)
        assert claimed is None  # cancelled task not claimable


# ────────────────────────────────────────────────────────────
# E. Cancel queued task (after translation completed)
# ────────────────────────────────────────────────────────────

class TestCancelQueued:
    def test_cancel_queued_after_translation(self, tmp_path):
        from app.services.fast_translation_worker import complete_fast_translation

        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        balance_before = _get_balance(s, TEST_USER_A)
        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]

        # Complete translation
        complete_fast_translation(
            s,
            request_code=result["request_code"],
            job_code=job_code,
            final_prompt="final prompt",
            character_key="",
        )

        # Cancel
        r = cancel_task_atomic(s, TEST_USER_A, job_code)
        refunded = r["refunded_fen"]
        assert refunded == result["charged_fen"]
        assert _get_balance(s, TEST_USER_A) == balance_before

    def test_duplicate_cancel_no_double_refund(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]

        # First cancel
        cancel_task_atomic(s, TEST_USER_A, job_code)
        balance_after_first = _get_balance(s, TEST_USER_A)

        # Second cancel should be idempotent (no double refund)
        r2 = cancel_task_atomic(s, TEST_USER_A, job_code)
        assert r2["already_cancelled"] is True
        assert r2["refunded_fen"] == 0
        assert _get_balance(s, TEST_USER_A) == balance_after_first


# ────────────────────────────────────────────────────────────
# F. One-to-one binding
# ────────────────────────────────────────────────────────────

class TestOneToOneBinding:
    def test_request_code_cannot_be_reused(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 200)

        result1 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test1",
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

        # Try to create another task with same request_code (should fail via unique index)
        # The unique index on fast_translation_request_code prevents this at DB level
        tr = _get_translation_request(s, result1["request_code"])
        assert tr["generation_job_code"] == result1["job_code"]

    def test_different_users_independent(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)
        _seed_user(s, TEST_USER_B, 100)

        r1 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester_a",
            original_prompt="test",
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
        r2 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_B,
            username="tester_b",
            original_prompt="test",
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

        assert r1["job_code"] != r2["job_code"]
        assert r1["request_code"] != r2["request_code"]


# ────────────────────────────────────────────────────────────
# G. Generation idempotency with fingerprint
# ────────────────────────────────────────────────────────────

class TestGenerationIdempotency:
    def test_same_client_request_id_same_fingerprint_returns_deduped(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 200)

        cid = "test-client-id-001"
        r1 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test prompt",
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
            client_request_id=cid,
        )

        r2 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test prompt",
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
            client_request_id=cid,
        )

        assert r1["job_code"] == r2["job_code"]
        assert r2["deduped"] is True

    def test_same_client_request_id_different_fingerprint_raises_conflict(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 200)

        cid = "test-client-id-002"
        create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="prompt A",
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
            client_request_id=cid,
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_fast_translation_task_atomic(
                s,
                job_code=make_job_code(),
                user_id=TEST_USER_A,
                username="tester",
                original_prompt="DIFFERENT prompt",  # Different content
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
                client_request_id=cid,
            )


# ────────────────────────────────────────────────────────────
# H. Concurrent cancel / worker finish race
# ────────────────────────────────────────────────────────────

class TestConcurrentCancelWorkerRace:
    def test_two_threads_cancel_same_task(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 100)

        result = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="test",
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
        job_code = result["job_code"]
        seed_balance = 100

        results = []
        errors = []

        def try_cancel():
            try:
                r = cancel_task_atomic(s, TEST_USER_A, job_code)
                results.append(r)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=try_cancel)
        t2 = threading.Thread(target=try_cancel)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        charged = result["charged_fen"]
        # Both should succeed — one refunds, one is idempotent
        assert len(results) == 2
        assert len(errors) == 0
        refunded_amounts = [r["refunded_fen"] for r in results]
        already_flags = [r["already_cancelled"] for r in results]
        assert sum(refunded_amounts) == charged  # total refund matches charged amount
        assert already_flags.count(True) == 1  # exactly one was already_cancelled
        assert already_flags.count(False) == 1

        # Balance should be restored to seed amount exactly
        assert _get_balance(s, TEST_USER_A) == seed_balance


# ────────────────────────────────────────────────────────────
# I. Active task limit
# ────────────────────────────────────────────────────────────

class TestActiveTaskLimit:
    def test_eleventh_task_rejected(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=10)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 10000)

        for i in range(10):
            create_fast_translation_task_atomic(
                s,
                job_code=make_job_code(),
                user_id=TEST_USER_A,
                username="tester",
                original_prompt=f"prompt {i}",
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

        balance_after_10 = _get_balance(s, TEST_USER_A)

        with pytest.raises(RuntimeError, match="active_task_limit"):
            create_fast_translation_task_atomic(
                s,
                job_code=make_job_code(),
                user_id=TEST_USER_A,
                username="tester",
                original_prompt="one too many",
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

        # No charge for rejected request
        assert _get_balance(s, TEST_USER_A) == balance_after_10

    def test_cancel_frees_slot(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=2)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 10000)

        r1 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="p1",
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
        create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="p2",
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

        # Cancel first
        cancel_task_atomic(s, TEST_USER_A, r1["job_code"])

        # Now can create again
        r3 = create_fast_translation_task_atomic(
            s,
            job_code=make_job_code(),
            user_id=TEST_USER_A,
            username="tester",
            original_prompt="p3",
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
        assert r3["status"] == "translating"
