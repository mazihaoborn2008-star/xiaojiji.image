"""Fast translation runtime fail-closed tests (section 十五D).

Tests for:
- Missing DeepSeek key rejection
- Disabled fast translator rejection
- Existing unserviceable task cleanup
- Worker recovery without key
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
    connect,
    create_fast_translation_task_atomic,
    ensure_schema,
)
from app.services.fast_translation_worker import (
    claim_next_fast_translation,
    fail_unserviceable_fast_translation_tasks,
    recover_stale_fast_translation_tasks,
)

TEST_USER = "test-fc-user"


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
        "dev_username": "Fail Closed Tester",
        "session_secret": "test-session-secret-for-fc-32chars!!!",
        "jwt_secret": "test-jwt-secret-for-fc-testing-only!!!!",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_key",
        "deepseek_model": "test-model",
        "price_fen_per_image": 2,
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


def _get_task(settings, job_code):
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_tr(settings, request_code):
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM translation_requests WHERE request_code=?", (request_code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ============================================================
# A. Fast creation key check
# ============================================================
class TestFastCreationKeyCheck:
    """Fast translation creation rejects when key is missing."""

    def test_fast_enabled_key_empty_returns_503(self, tmp_path):
        """fast enabled + key empty → 503."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, deepseek_api_key="")
        _seed_balance(settings, TEST_USER, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/tasks", data={
                "prompt": "blue sky and white clouds",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1024,
                "translation_mode": "fast",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "fast_translator_unavailable"
        finally:
            app.dependency_overrides.clear()

    def test_fast_enabled_key_empty_no_charge(self, tmp_path):
        """Key empty → no charge."""
        settings = _make_settings(tmp_path, deepseek_api_key="")
        _seed_balance(settings, TEST_USER, 50000)
        bal_before = _get_balance(settings, TEST_USER)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/tasks", data={
                "prompt": "blue sky and white clouds",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1024,
                "translation_mode": "fast",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 503
            assert _get_balance(settings, TEST_USER) == bal_before
        finally:
            app.dependency_overrides.clear()

    def test_fast_disabled_returns_400(self, tmp_path):
        """fast disabled → 400."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, fast_translator_enabled=False)
        _seed_balance(settings, TEST_USER, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/tasks", data={
                "prompt": "blue sky and white clouds",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1024,
                "translation_mode": "fast",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 400
        finally:
            app.dependency_overrides.clear()

    def test_test_only_key_works_in_local(self, tmp_path):
        """TEST_ONLY key works in local environment."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, deepseek_api_key="TEST_ONLY_browser")
        _seed_balance(settings, TEST_USER, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/tasks", data={
                "prompt": "blue sky and white clouds",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1024,
                "translation_mode": "fast",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            assert r.json()["status"] == "translating"
        finally:
            app.dependency_overrides.clear()


# ============================================================
# B. Existing unserviceable tasks
# ============================================================
class TestUnserviceableTasks:
    """Existing tasks that can't be processed are failed and refunded."""

    def test_queued_task_no_key_failed(self, tmp_path):
        """Queued fast task with no key → failed_refunded."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)
        bal_before = _get_balance(settings, TEST_USER)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"UNQ-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            original_prompt="blue sky",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Now clear the key
        settings.deepseek_api_key = ""
        failed = fail_unserviceable_fast_translation_tasks(settings)
        assert failed == 1

        task = _get_task(settings, gen["job_code"])
        assert task["status"] == "failed_refunded"
        assert _get_balance(settings, TEST_USER) == bal_before

    def test_processing_task_no_key_failed(self, tmp_path):
        """Processing fast task with no key → failed_refunded."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)
        bal_before = _get_balance(settings, TEST_USER)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"UNP-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            original_prompt="blue sky",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Claim it
        claim_next_fast_translation(settings)

        # Clear key
        settings.deepseek_api_key = ""
        failed = fail_unserviceable_fast_translation_tasks(settings)
        assert failed == 1

        task = _get_task(settings, gen["job_code"])
        assert task["status"] == "failed_refunded"
        assert _get_balance(settings, TEST_USER) == bal_before

    def test_no_double_refund_on_cancel_race(self, tmp_path):
        """Cancel race: unserviceable fail doesn't double refund."""
        from app.db import cancel_task_atomic

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)
        bal_before = _get_balance(settings, TEST_USER)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"RC-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            original_prompt="blue sky",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # User cancels first
        cancel_task_atomic(settings, TEST_USER, gen["job_code"])

        # Clear key and try to fail
        settings.deepseek_api_key = ""
        failed = fail_unserviceable_fast_translation_tasks(settings)
        # Should not fail already-cancelled task
        assert failed == 0

        # Balance should be fully restored (only one refund)
        assert _get_balance(settings, TEST_USER) == bal_before


# ============================================================
# C. Worker recovery without key
# ============================================================
class TestWorkerRecoveryWithoutKey:
    """Worker runs recovery even without DeepSeek key."""

    def test_recovery_runs_without_key(self, tmp_path):
        """Recovery works even when deepseek_api_key is empty."""
        settings = _make_settings(tmp_path, deepseek_api_key="")
        _seed_balance(settings, TEST_USER, 50000)

        # Recovery should not crash without key
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0  # no stale tasks
