"""Translation mode compatibility tests (section 十五A-B).

Tests for:
A. translation_mode resolution
B. Normal translation billing and fields
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.db import connect, create_task_atomic, ensure_schema
from app.services.fast_translator_service import CharacterSelectionRequired

TEST_USER = "test-mode-user"


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
        "dev_username": "Mode Tester",
        "session_secret": "test-session-secret-for-mode-32chars!!!!!",
        "jwt_secret": "test-jwt-secret-for-mode-testing-only!!!",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_key",
        "deepseek_model": "test-model",
        "price_fen_per_image": 2,
        "agent_enabled": True,
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


# ============================================================
# A. translation_mode resolution via HTTP
# ============================================================
class TestTranslationModeResolution:
    """Test server-side translation_mode resolution."""

    def test_explicit_fast(self, tmp_path):
        """Explicit fast → fast mode."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
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
            task = _get_task(settings, r.json()["job_code"])
            assert task["translation_mode"] == "fast"
        finally:
            app.dependency_overrides.clear()

    def test_explicit_normal_agent_enabled(self, tmp_path):
        """Explicit normal + agent enabled → normal."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=True)
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
                "translation_mode": "normal",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["translation_mode"] == "normal"
            assert task["use_agent"] == 1
        finally:
            app.dependency_overrides.clear()

    def test_explicit_normal_agent_disabled(self, tmp_path):
        """Explicit normal + agent disabled → 503."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=False)
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
                "translation_mode": "normal",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "agent_unavailable"
        finally:
            app.dependency_overrides.clear()

    def test_explicit_none_with_use_agent(self, tmp_path):
        """Explicit none + use_agent=true → none (explicit wins)."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=True)
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
                "translation_mode": "none",
                "use_agent": "true",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["translation_mode"] == "none"
            assert task["use_agent"] == 0
        finally:
            app.dependency_overrides.clear()

    def test_no_mode_use_agent_enabled(self, tmp_path):
        """No mode + use_agent=true + agent enabled → normal (legacy compat)."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=True)
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
                "use_agent": "true",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["translation_mode"] == "normal"
            assert task["use_agent"] == 1
        finally:
            app.dependency_overrides.clear()

    def test_no_mode_use_agent_disabled(self, tmp_path):
        """No mode + use_agent=true + agent disabled → 503."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=False)
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
                "use_agent": "true",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "agent_unavailable"
        finally:
            app.dependency_overrides.clear()

    def test_no_mode_no_use_agent(self, tmp_path):
        """No mode + use_agent=false → none."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=True)
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
                "use_agent": "false",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["translation_mode"] == "none"
            assert task["use_agent"] == 0
        finally:
            app.dependency_overrides.clear()

    def test_invalid_mode(self, tmp_path):
        """Invalid mode → 400."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
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
                "translation_mode": "hacked",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 400
            assert r.json()["detail"]["code"] == "invalid_translation_mode"
        finally:
            app.dependency_overrides.clear()

    def test_client_prompt_source_ignored(self, tmp_path):
        """Client prompt_source is ignored for normal mode."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, agent_enabled=False)
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
                "prompt_source": "fast_translate:FORGED",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["prompt_source"] == "user_raw"
        finally:
            app.dependency_overrides.clear()


# ============================================================
# B. Normal translation billing
# ============================================================
class TestNormalTranslationBilling:
    """Test normal translation mode billing and fields."""

    def test_normal_mode_charges_agent_surcharge(self, tmp_path):
        """Normal mode charges base + agent surcharge."""
        settings = _make_settings(tmp_path, agent_enabled=True, agent_surcharge_credits=1)
        _seed_balance(settings, TEST_USER, 50000)
        bal_before = _get_balance(settings, TEST_USER)

        result = create_task_atomic(
            settings,
            job_code=f"NB-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            prompt="blue sky and white clouds",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
            use_agent=True,
            translation_mode="normal",
        )
        task = _get_task(settings, result["job_code"])
        assert task["translation_mode"] == "normal"
        assert task["use_agent"] == 1
        # base(2) + agent_surcharge(1) = 3
        assert task["charged_fen"] == 3
        assert _get_balance(settings, TEST_USER) == bal_before - 3

    def test_normal_mode_prompt_source_server_generated(self, tmp_path):
        """Normal mode prompt_source is server-generated."""
        settings = _make_settings(tmp_path, agent_enabled=True)
        _seed_balance(settings, TEST_USER, 50000)

        result = create_task_atomic(
            settings,
            job_code=f"NP-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            prompt="blue sky and white clouds",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
            use_agent=True,
            prompt_source="agent_no_character",
            translation_mode="normal",
        )
        task = _get_task(settings, result["job_code"])
        assert task["prompt_source"] == "agent_no_character"

    def test_queued_at_set_for_normal_tasks(self, tmp_path):
        """Normal queued tasks have queued_at=created_at."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER, 50000)

        result = create_task_atomic(
            settings,
            job_code=f"QA-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER,
            username="Test",
            prompt="test",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        task = _get_task(settings, result["job_code"])
        assert task["queued_at"] is not None
        assert task["queued_at"] == task["created_at"]
