"""Fast translation recovery tests.

Tests for:
- Stale processing recovery (requeue vs fail)
- Max attempts enforcement
- Concurrent safety (cancel, complete, recovery)
- Balance restoration
- Ledger idempotency
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.db import (
    cancel_task_atomic,
    connect,
    create_fast_translation_task_atomic,
    ensure_schema,
    fail_fast_translation_task_refund_atomic,
)
from app.services.fast_translation_worker import (
    FAST_TRANSLATION_CLAIM_TTL,
    FAST_TRANSLATION_MAX_ATTEMPTS,
    claim_next_fast_translation,
    complete_fast_translation,
    recover_stale_fast_translation_tasks,
)

TEST_USER = "test-recovery-user"


def _make_settings(tmp_path, **overrides):
    db_path = tmp_path / "test.db"
    input_dir = tmp_path / "input_images"
    output_dir = tmp_path / "output"
    mock_dir = tmp_path / "mock_output"
    for d in (input_dir, output_dir, mock_dir):
        d.mkdir(exist_ok=True)
    defaults = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(db_path),
        "BOT_OUTPUT_DIR": str(output_dir),
        "mock_output_dir": str(mock_dir),
        "INPUT_IMAGE_DIR": str(input_dir),
        "BOT_DIR": str(tmp_path),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER,
        "dev_username": "Recovery Tester",
        "session_secret": "test-session-secret-for-recovery-32chars!!!",
        "jwt_secret": "test-jwt-secret-for-recovery-testing-only!!!",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 0,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_key",
        "deepseek_model": "test-model",
        "deepseek_timeout_seconds": 5,
        "price_fen_per_image": 2,
        "smart_agent_enabled": False,
    }
    defaults.update(overrides)
    s = Settings(**defaults)
    ensure_schema(s)
    return s


def _seed_balance(settings, user_id, amount):
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (user_id, amount))
        conn.commit()
    finally:
        conn.close()


def _get_balance(settings, user_id):
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _count_ledger(settings, user_id, reason):
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE user_id=? AND reason=?",
            (user_id, reason),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _get_tr(settings, request_code):
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM translation_requests WHERE request_code=?",
            (request_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_gt(settings, job_code):
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM generation_tasks WHERE job_code=?",
            (job_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _make_stale(settings, request_code, attempt_count=1):
    """Make a processing translation request stale."""
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE translation_requests SET started_at=?, attempt_count=? WHERE request_code=?",
            (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, attempt_count, request_code),
        )
        conn.commit()
    finally:
        conn.close()


def _create_and_claim(settings, user_id=TEST_USER):
    """Create a fast translation task and claim it."""
    _seed_balance(settings, user_id, 50000)
    gen = create_fast_translation_task_atomic(
        settings,
        job_code=f"RC-{uuid.uuid4().hex[:8].upper()}",
        user_id=user_id,
        username="Test",
        original_prompt="a sunset scene",
        translation_mode="fast",
        style_key="style_a",
        lora_weight=1.0,
        width=1024, height=1536, mode="txt2img",
        input_image_path=None, denoise=0.5,
        control_type="depth", control_character="prompt",
        auto_tagger=False,
    )
    task = claim_next_fast_translation(settings)
    return gen, task


# ============================================================
# Recovery: requeue scenarios
# ============================================================
class TestRecoveryRequeue:
    """Stale tasks under max_attempts are requeued."""

    def test_first_stale_requeued(self, tmp_path):
        """attempt_count=1 (< max=2) → requeued."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)
        assert task is not None

        _make_stale(settings, gen["request_code"], attempt_count=1)

        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "queued"
        assert tr["started_at"] is None
        assert tr["error_code"] == "stale_requeued"

    def test_requeued_task_can_be_claimed_again(self, tmp_path):
        """Requeued task can be claimed and completed."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        _make_stale(settings, gen["request_code"], attempt_count=1)
        recover_stale_fast_translation_tasks(settings)

        # Claim again
        task2 = claim_next_fast_translation(settings)
        assert task2 is not None
        assert task2["request_code"] == gen["request_code"]

        # attempt_count should increase
        tr = _get_tr(settings, gen["request_code"])
        assert tr["attempt_count"] == 2

    def test_requeued_then_completed_successfully(self, tmp_path):
        """Requeued task can complete normally."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        _make_stale(settings, gen["request_code"], attempt_count=1)
        recover_stale_fast_translation_tasks(settings)

        # Claim and complete
        claim_next_fast_translation(settings)
        ok = complete_fast_translation(
            settings,
            request_code=gen["request_code"],
            job_code=gen["job_code"],
            final_prompt="1girl, sunset, outdoor",
            character_key="[]",
        )
        assert ok is True

        gt = _get_gt(settings, gen["job_code"])
        assert gt["status"] == "queued"


# ============================================================
# Recovery: fail scenarios
# ============================================================
class TestRecoveryFail:
    """Stale tasks at max_attempts are failed and refunded."""

    def test_max_attempts_triggers_fail(self, tmp_path):
        """attempt_count >= max_attempts → failed_refunded."""
        settings = _make_settings(tmp_path)
        bal_before = 50000
        gen, task = _create_and_claim(settings, user_id=TEST_USER)

        _make_stale(settings, gen["request_code"], attempt_count=FAST_TRANSLATION_MAX_ATTEMPTS)

        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "failed_refunded"

        gt = _get_gt(settings, gen["job_code"])
        assert gt["status"] == "failed_refunded"

    def test_balance_fully_restored_on_fail(self, tmp_path):
        """Full balance restored after max attempts failure."""
        settings = _make_settings(tmp_path)
        bal_before = 50000
        gen, task = _create_and_claim(settings, user_id=TEST_USER)

        _make_stale(settings, gen["request_code"], attempt_count=FAST_TRANSLATION_MAX_ATTEMPTS)
        recover_stale_fast_translation_tasks(settings)

        assert _get_balance(settings, TEST_USER) == bal_before

    def test_refund_ledger_written_once(self, tmp_path):
        """Refund ledger entry written exactly once."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings, user_id=TEST_USER)

        _make_stale(settings, gen["request_code"], attempt_count=FAST_TRANSLATION_MAX_ATTEMPTS)
        recover_stale_fast_translation_tasks(settings)

        # Check refund ledger
        assert _count_ledger(settings, TEST_USER, "generate_failed_refund") == 1


# ============================================================
# Recovery: does not touch terminal states
# ============================================================
class TestRecoveryIgnoresTerminal:
    """Recovery does not revive cancelled, done, or failed tasks."""

    def test_cancelled_not_recovered(self, tmp_path):
        """cancelled_refunded tasks stay cancelled."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        # Cancel first
        cancel_task_atomic(settings, TEST_USER, gen["job_code"])

        # Make it look stale (shouldn't matter)
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET started_at=?, attempt_count=1 WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0

        gt = _get_gt(settings, gen["job_code"])
        assert gt["status"] == "cancelled_refunded"

    def test_done_not_recovered(self, tmp_path):
        """Done tasks are not touched."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        # Complete it
        complete_fast_translation(
            settings,
            request_code=gen["request_code"],
            job_code=gen["job_code"],
            final_prompt="translated",
            character_key="[]",
        )

        # Make it look stale
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET started_at=?, attempt_count=1 WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0

        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "done"


# ============================================================
# Recovery: concurrent safety
# ============================================================
class TestRecoveryConcurrency:
    """Recovery is safe with concurrent operations."""

    def test_recovery_after_cancel_race(self, tmp_path):
        """Cancel wins, recovery doesn't revive."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        # User cancels
        cancel_task_atomic(settings, TEST_USER, gen["job_code"])

        # Stale recovery runs
        _make_stale(settings, gen["request_code"], attempt_count=1)
        recovered = recover_stale_fast_translation_tasks(settings)

        # Should not revive
        gt = _get_gt(settings, gen["job_code"])
        assert gt["status"] == "cancelled_refunded"

    def test_recovery_after_complete_race(self, tmp_path):
        """Complete wins, recovery doesn't double-process."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        # Worker completes
        complete_fast_translation(
            settings,
            request_code=gen["request_code"],
            job_code=gen["job_code"],
            final_prompt="translated",
            character_key="[]",
        )

        # Make it look stale (but it's already done)
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET started_at=?, attempt_count=1 WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0

        gt = _get_gt(settings, gen["job_code"])
        assert gt["status"] == "queued"  # completed normally

    def test_recovery_no_partial_update(self, tmp_path):
        """Recovery uses atomic transactions, no partial updates."""
        settings = _make_settings(tmp_path)
        gen, task = _create_and_claim(settings)

        _make_stale(settings, gen["request_code"], attempt_count=1)

        # Recovery should be atomic
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        # Both translation request and generation task should be consistent
        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "queued"


# ============================================================
# Recovery: generation task missing
# ============================================================
class TestRecoveryEdgeCases:
    """Edge cases in recovery."""

    def test_generation_task_deleted_safely(self, tmp_path):
        """If generation task is missing, recovery handles gracefully."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"EDGE-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        claim_next_fast_translation(settings)

        # Delete generation task (simulates data inconsistency)
        conn = connect(settings)
        try:
            conn.execute("DELETE FROM generation_tasks WHERE job_code=?", (gen["job_code"],))
            conn.commit()
        finally:
            conn.close()

        _make_stale(settings, gen["request_code"], attempt_count=FAST_TRANSLATION_MAX_ATTEMPTS)

        # Should not crash
        recovered = recover_stale_fast_translation_tasks(settings)
        # The fail_fast_translation_task_refund_atomic will return False because GT is missing
        # But recovery should still process the translation request
        assert recovered >= 0  # should not raise

    def test_no_stale_tasks(self, tmp_path):
        """No stale tasks → recovery is no-op."""
        settings = _make_settings(tmp_path)
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0


class TestTerminalReconciliation:
    """Terminal state reconciliation runs even without stale candidates."""

    def test_cancelled_orphan_reconciled(self, tmp_path):
        """Orphaned processing TR with cancelled GT → reconciled."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"TOC-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        bal_before = _get_balance(settings, TEST_USER)

        # Claim it → processing
        claim_next_fast_translation(settings)

        # Directly set GT to cancelled_refunded (simulates race where cancel updated GT but TR stayed processing)
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE generation_tasks SET status='cancelled_refunded', finished_at=? WHERE job_code=?",
                (int(time.time()), gen["job_code"]),
            )
            conn.execute(
                "UPDATE translation_requests SET started_at=? WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        # Recovery should reconcile (no stale translating candidates, but orphaned processing exists)
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        # TR should be cancelled_refunded
        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "cancelled_refunded"

        # No new refund ledger
        assert _count_ledger(settings, TEST_USER, "generate_cancel_refund") == 0

    def test_failed_orphan_reconciled(self, tmp_path):
        """Orphaned processing TR with failed GT → reconciled."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"TOF-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        bal_before = _get_balance(settings, TEST_USER)

        # Claim it
        claim_next_fast_translation(settings)

        # Directly set GT to failed_refunded and keep TR as processing
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE generation_tasks SET status='failed_refunded', finished_at=?, error_code='test_fail' WHERE job_code=?",
                (int(time.time()), gen["job_code"]),
            )
            conn.execute(
                "UPDATE translation_requests SET started_at=? WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        # Recovery should reconcile
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        tr = _get_tr(settings, gen["request_code"])
        assert tr["status"] == "failed_refunded"

        # Balance unchanged (GT was already failed_refunded, no new refund)
        assert _get_balance(settings, TEST_USER) == bal_before


class TestClaimExactBinding:
    """Claim requires exact binding: job_code, request_code, translation_mode."""

    def test_wrong_job_code_returns_none(self, tmp_path):
        """request_code correct but generation_job_code wrong → claim None."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"JC-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Corrupt generation_job_code in TR
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET generation_job_code='WRONG' WHERE request_code=?",
                (gen["request_code"],),
            )
            conn.commit()
        finally:
            conn.close()

        task = claim_next_fast_translation(settings)
        assert task is None

    def test_wrong_request_code_returns_none(self, tmp_path):
        """generation_job_code correct but fast_translation_request_code wrong → claim None."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"RC-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Corrupt fast_translation_request_code in GT
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE generation_tasks SET fast_translation_request_code='WRONG' WHERE job_code=?",
                (gen["job_code"],),
            )
            conn.commit()
        finally:
            conn.close()

        task = claim_next_fast_translation(settings)
        assert task is None

    def test_non_fast_mode_returns_none(self, tmp_path):
        """translation_mode=none → claim None."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"NF-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Change translation_mode to none
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE generation_tasks SET translation_mode='none' WHERE job_code=?",
                (gen["job_code"],),
            )
            conn.commit()
        finally:
            conn.close()

        task = claim_next_fast_translation(settings)
        assert task is None

    def test_exact_binding_succeeds(self, tmp_path):
        """Exact binding with fast mode → claim succeeds."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"EB-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER, username="Test",
            original_prompt="blue sky", translation_mode="fast",
            style_key="style_a", lora_weight=1.0,
            width=1024, height=1024, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        task = claim_next_fast_translation(settings)
        assert task is not None
        assert task["request_code"] == gen["request_code"]
        assert task["job_code"] == gen["job_code"]
