"""Deployment blocker hardening tests (sections 十九A-J).

Tests for:
A. Legacy fast_translation_request_code binding rejection
B. prompt_source server authority
C. Strict character confirmation validation
D. Generation fingerprint idempotency
E. Upload file content hash
F. Cancel fallback removal
G. Stale processing recovery
H. created_at / queued_at
I. Web restart recovery
J. Smoke scripts
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.db import (
    cancel_task_atomic,
    connect,
    create_fast_translation_task_atomic,
    create_task_atomic,
    ensure_schema,
    fail_fast_translation_task_refund_atomic,
)
from app.services.fast_translator_service import (
    CharacterSelectionRequired,
    FastTranslatorError,
    _resolve_characters,
    fast_refine_prompt,
)
from app.services.fast_translation_worker import (
    FAST_TRANSLATION_CLAIM_TTL,
    FAST_TRANSLATION_MAX_ATTEMPTS,
    claim_next_fast_translation,
    complete_fast_translation,
    recover_stale_fast_translation_tasks,
)

TEST_USER_A = "test-user-a"
TEST_USER_B = "test-user-b"


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
        "dev_user_id": TEST_USER_A,
        "dev_username": "Test User",
        "session_secret": "test-session-secret-for-hardening-32chars!!",
        "jwt_secret": "test-jwt-secret-for-hardening-testing-only",
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


def _get_task(settings, job_code):
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM generation_tasks WHERE job_code=?", (job_code,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# A. Legacy fast_translation_request_code binding rejection
# ============================================================
class TestLegacyRequestCodeRejection:
    """Old fast_translation_request_code cannot create generation tasks."""

    def test_create_task_atomic_no_request_code_param(self, tmp_path):
        """create_task_atomic does not accept fast_translation_request_code."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        import inspect
        sig = inspect.signature(create_task_atomic)
        assert "fast_translation_request_code" not in sig.parameters

    def test_http_rejects_legacy_request_code(self, tmp_path):
        """POST /api/tasks with fast_translation_request_code returns 400."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            # Create a translation request first
            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "a sunset",
                "client_request_id": f"legacy-{uuid.uuid4().hex[:8]}",
            }, headers={"X-CSRF-Token": "test"})
            assert r1.status_code == 200
            ft_code = r1.json()["request_code"]

            # Try to use it in /api/tasks
            r2 = client.post("/api/tasks", data={
                "prompt": "a sunset",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1536,
                "fast_translation_request_code": ft_code,
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 400
            detail = r2.json()["detail"]
            assert detail["code"] == "legacy_fast_translation_binding_not_supported"
        finally:
            app.dependency_overrides.clear()

    def test_legacy_code_no_charge(self, tmp_path):
        """Legacy request_code rejection does not charge."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            bal_before = _get_balance(settings, TEST_USER_A)

            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "a sunset",
                "client_request_id": f"legacy-{uuid.uuid4().hex[:8]}",
            }, headers={"X-CSRF-Token": "test"})
            ft_code = r1.json()["request_code"]

            r2 = client.post("/api/tasks", data={
                "prompt": "a sunset",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1536,
                "fast_translation_request_code": ft_code,
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 400

            # Balance should only reflect the fast-refine charge, not a generation charge
            bal_after = _get_balance(settings, TEST_USER_A)
            assert bal_before - bal_after == 2  # only translation charge
        finally:
            app.dependency_overrides.clear()


# ============================================================
# B. prompt_source server authority
# ============================================================
class TestPromptSourceAuthority:
    """prompt_source is server-authoritative, client value ignored."""

    def test_create_task_atomic_stores_provided_source(self, tmp_path):
        """create_task_atomic stores the prompt_source passed by caller (HTTP layer)."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)

        result = create_task_atomic(
            settings,
            job_code=f"PS-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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
            prompt_source="user_raw",
        )
        task = _get_task(settings, result["job_code"])
        assert task["prompt_source"] == "user_raw"

    def test_fast_mode_server_source(self, tmp_path):
        """Fast translation uses server-generated prompt_source."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)

        result = create_fast_translation_task_atomic(
            settings,
            job_code=f"FS-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
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
        task = _get_task(settings, result["job_code"])
        assert task["prompt_source"].startswith("fast_translate_pending:")

    def test_http_none_mode_ignores_client_prompt_source(self, tmp_path):
        """HTTP /api/tasks ignores client prompt_source in none mode."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/tasks", data={
                "prompt": "test prompt",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1536,
                "use_agent": "false",
                "prompt_source": "fast_translate:FORGED",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            task = _get_task(settings, r.json()["job_code"])
            assert task["prompt_source"] == "user_raw"
        finally:
            app.dependency_overrides.clear()


# ============================================================
# C. Strict character confirmation validation
# ============================================================
class TestStrictCharacterValidation:
    """Character confirmation uses strict shared validation."""

    def test_ambiguous_no_resolution_returns_409(self, tmp_path):
        """Ambiguous prompt without resolution → 409, no charge."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        # "麻美" is ambiguous (七海麻美 vs 巴麻美)
        with pytest.raises(CharacterSelectionRequired):
            _resolve_characters("麻美穿风衣", None)

        # Balance unchanged
        assert _get_balance(settings, TEST_USER_A) == bal_before

    def test_empty_selections_rejected(self, tmp_path):
        """Empty selections dict is treated as invalid, not 'none of above'."""
        settings = _make_settings(tmp_path)
        resolution = {"status": "resolved", "selections": []}
        # "麻美穿风衣" is ambiguous; empty selections should fail
        with pytest.raises((FastTranslatorError, CharacterSelectionRequired)):
            _resolve_characters("麻美穿风衣", resolution)

    def test_missing_mention_rejected(self, tmp_path):
        """Resolution missing a mention → rejected."""
        settings = _make_settings(tmp_path)
        resolution = {
            "status": "resolved",
            "selections": [{"mention": "铃鹿", "characterId": "silence_suzuka"}],
        }
        # Prompt has both characters but resolution only covers one
        with pytest.raises((FastTranslatorError, CharacterSelectionRequired)):
            _resolve_characters("铃鹿和帝王在赛跑", resolution)

    def test_forged_character_id_rejected(self, tmp_path):
        """Forged characterId not in library → rejected."""
        settings = _make_settings(tmp_path)
        resolution = {
            "status": "resolved",
            "selections": [{"mention": "某角色", "characterId": "FORGED_ID_12345"}],
        }
        with pytest.raises((FastTranslatorError, CharacterSelectionRequired)):
            _resolve_characters("某角色在跑步", resolution)

    def test_valid_none_of_above_accepted(self, tmp_path):
        """Legitimate 'none of above' → character_keys=[], decision=none."""
        from app.smart_agent.disambiguation_engine import NO_LIBRARY_CHARACTER_ID
        settings = _make_settings(tmp_path)
        resolution = {
            "status": "resolved",
            "selections": [{"mention": "蝴蝶结", "characterId": NO_LIBRARY_CHARACTER_ID}],
        }
        keys, decision = _resolve_characters("蝴蝶结在飞", resolution)
        assert keys == []
        assert decision == "none"

    def test_http_ambiguous_no_charge(self, tmp_path):
        """HTTP ambiguous prompt → 409, no generation charge."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            bal_before = _get_balance(settings, TEST_USER_A)

            # "麻美穿风衣" is ambiguous (七海麻美 vs 巴麻美)
            r = client.post("/api/tasks", data={
                "prompt": "麻美穿风衣",
                "mode": "txt2img",
                "style_key": "style_a",
                "width": 1024,
                "height": 1536,
                "translation_mode": "fast",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 409
            assert r.json()["detail"]["code"] == "character_resolution_required"

            # No charge
            assert _get_balance(settings, TEST_USER_A) == bal_before
        finally:
            app.dependency_overrides.clear()


# ============================================================
# D. Generation fingerprint idempotency
# ============================================================
class TestGenerationFingerprint:
    """Fingerprint-based idempotency and conflict detection."""

    def test_same_id_same_payload_dedup(self, tmp_path):
        """Same client_request_id + same payload → deduped."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"fp-dedup-{uuid.uuid4().hex[:8]}"

        r1 = create_task_atomic(
            settings,
            job_code=f"FP1-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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
            request_fingerprint="abc123",
        )
        r2 = create_task_atomic(
            settings,
            job_code=f"FP2-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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
            request_fingerprint="abc123",
        )
        assert r2["deduped"] is True
        assert r1["job_code"] == r2["job_code"]

    def test_same_id_different_fingerprint_conflict(self, tmp_path):
        """Same client_request_id + different fingerprint → 409."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"fp-conflict-{uuid.uuid4().hex[:8]}"

        create_task_atomic(
            settings,
            job_code=f"FC1-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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
            request_fingerprint="abc123",
        )

        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_task_atomic(
                settings,
                job_code=f"FC2-{uuid.uuid4().hex[:8].upper()}",
                user_id=TEST_USER_A,
                username="Test",
                prompt="DIFFERENT prompt",
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
                request_fingerprint="DIFFERENT",
            )

    def test_legacy_empty_fingerprint_conflict(self, tmp_path):
        """Old task with empty fingerprint → safely return conflict."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"fp-legacy-{uuid.uuid4().hex[:8]}"

        # Create task WITHOUT fingerprint (simulates legacy)
        create_task_atomic(
            settings,
            job_code=f"FL1-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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
            request_fingerprint="",
        )

        # New request with fingerprint → conflict (safe behavior)
        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_task_atomic(
                settings,
                job_code=f"FL2-{uuid.uuid4().hex[:8].upper()}",
                user_id=TEST_USER_A,
                username="Test",
                prompt="a sunset",
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
                request_fingerprint="new_fingerprint",
            )

    def test_different_style_conflict(self, tmp_path):
        """Same ID, different style → conflict."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"fp-style-{uuid.uuid4().hex[:8]}"

        create_task_atomic(
            settings, job_code=f"S1-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A, username="Test", prompt="a sunset",
            style_key="style_a", lora_weight=1.0, width=1024, height=1536,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt", auto_tagger=False,
            client_request_id=cid, request_fingerprint="fp_a",
        )
        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_task_atomic(
                settings, job_code=f"S2-{uuid.uuid4().hex[:8].upper()}",
                user_id=TEST_USER_A, username="Test", prompt="a sunset",
                style_key="style_b", lora_weight=1.0, width=1024, height=1536,
                mode="txt2img", input_image_path=None, denoise=0.5,
                control_type="depth", control_character="prompt", auto_tagger=False,
                client_request_id=cid, request_fingerprint="fp_b",
            )

    def test_different_width_conflict(self, tmp_path):
        """Same ID, different width → conflict."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"fp-width-{uuid.uuid4().hex[:8]}"

        create_task_atomic(
            settings, job_code=f"W1-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A, username="Test", prompt="a sunset",
            style_key="style_a", lora_weight=1.0, width=1024, height=1024,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt", auto_tagger=False,
            client_request_id=cid, request_fingerprint="fp_w",
        )
        with pytest.raises(ValueError, match="client_request_id_conflict"):
            create_task_atomic(
                settings, job_code=f"W2-{uuid.uuid4().hex[:8].upper()}",
                user_id=TEST_USER_A, username="Test", prompt="a sunset",
                style_key="style_a", lora_weight=1.0, width=768, height=768,
                mode="txt2img", input_image_path=None, denoise=0.5,
                control_type="depth", control_character="prompt", auto_tagger=False,
                client_request_id=cid, request_fingerprint="fp_w2",
            )


# ============================================================
# E. Upload file content hash
# ============================================================
class TestUploadContentHash:
    """Same content → same hash; different content → different hash."""

    def test_same_content_same_hash(self):
        """SHA-256 of same bytes is identical."""
        data = b"fake image content for testing"
        h1 = hashlib.sha256(data).hexdigest()
        h2 = hashlib.sha256(data).hexdigest()
        assert h1 == h2

    def test_different_content_different_hash(self):
        """SHA-256 of different bytes differs."""
        h1 = hashlib.sha256(b"content A").hexdigest()
        h2 = hashlib.sha256(b"content B").hexdigest()
        assert h1 != h2


# ============================================================
# F. Cancel fallback removal
# ============================================================
class TestCancelFallbackRemoval:
    """Cancel no longer guesses translation relationship."""

    def test_cancel_independent_translation_no_refund(self, tmp_path):
        """Cancel task without ft_code → only generation refund."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        # Create independent translation request
        cid = f"cancel-indie-{uuid.uuid4().hex[:8]}"
        _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A,
            text="a sunset",
            client_request_id=cid,
        ))

        # Create generation task with same client_request_id but no ft_code
        gen = create_task_atomic(
            settings,
            job_code=f"GEN-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="a sunset",
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

        r = cancel_task_atomic(settings, TEST_USER_A, gen["job_code"])
        # Only generation refund (2), not translation (2)
        assert r["refunded_fen"] == 2
        assert _count_ledger(settings, TEST_USER_A, "fast_translate_cancel_refund") == 0

    def test_fast_task_full_refund(self, tmp_path):
        """Fast translation task → full bundled refund."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"FT-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
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

        r = cancel_task_atomic(settings, TEST_USER_A, gen["job_code"])
        assert r["refunded_fen"] == 4  # gen(2) + translation(2)
        assert _get_balance(settings, TEST_USER_A) == bal_before


# ============================================================
# G. Stale processing recovery
# ============================================================
class TestStaleProcessingRecovery:
    """Recovery of stale processing translation tasks."""

    def test_processing_not_yet_stale(self, tmp_path):
        """Processing task within TTL → not recovered."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"ST-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Claim it
        task = claim_next_fast_translation(settings)
        assert task is not None

        # Recovery should not process it (just claimed, within TTL)
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0

    def test_stale_requeued_when_under_max_attempts(self, tmp_path):
        """Stale processing with attempt < max → requeued."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"SR-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        request_code = gen["request_code"]

        # Claim it
        task = claim_next_fast_translation(settings)
        assert task is not None

        # Simulate stale: set started_at to past
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET started_at=? WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, request_code),
            )
            conn.commit()
        finally:
            conn.close()

        # Recovery should requeue (attempt_count=1 < max_attempts=2)
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        # Verify: translation request back to queued
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT status, started_at, error_code FROM translation_requests WHERE request_code=?",
                (request_code,),
            ).fetchone()
            assert row["status"] == "queued"
            assert row["started_at"] is None
            assert row["error_code"] == "stale_requeued"
        finally:
            conn.close()

    def test_stale_failed_when_at_max_attempts(self, tmp_path):
        """Stale processing with attempt >= max → failed_refunded."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"SF-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        request_code = gen["request_code"]

        # Claim and simulate max attempts
        task = claim_next_fast_translation(settings)
        assert task is not None

        # Set attempt_count to max and make stale
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET attempt_count=?, started_at=? WHERE request_code=?",
                (FAST_TRANSLATION_MAX_ATTEMPTS, int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, request_code),
            )
            conn.commit()
        finally:
            conn.close()

        # Recovery should fail and refund
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        # Verify: both failed_refunded
        conn = connect(settings)
        try:
            tr = conn.execute(
                "SELECT status FROM translation_requests WHERE request_code=?",
                (request_code,),
            ).fetchone()
            assert tr["status"] == "failed_refunded"

            gt = conn.execute(
                "SELECT status FROM generation_tasks WHERE job_code=?",
                (gen["job_code"],),
            ).fetchone()
            assert gt["status"] == "failed_refunded"
        finally:
            conn.close()

        # Balance fully restored
        assert _get_balance(settings, TEST_USER_A) == bal_before

    def test_cancelled_refunded_not_recovered(self, tmp_path):
        """cancelled_refunded tasks are not revived by recovery."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"SC-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Cancel it
        cancel_task_atomic(settings, TEST_USER_A, gen["job_code"])

        # Recovery should not touch it
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 0

        conn = connect(settings)
        try:
            gt = conn.execute(
                "SELECT status FROM generation_tasks WHERE job_code=?",
                (gen["job_code"],),
            ).fetchone()
            assert gt["status"] == "cancelled_refunded"
        finally:
            conn.close()


# ============================================================
# H. created_at / queued_at
# ============================================================
class TestTimestamps:
    """created_at immutable; queued_at set on translation complete."""

    def test_fast_task_created_at_set(self, tmp_path):
        """Fast translation task has created_at, queued_at=NULL."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"TS-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        task = _get_task(settings, gen["job_code"])
        assert task["created_at"] > 0
        assert task["queued_at"] is None

    def test_complete_sets_queued_at_preserves_created_at(self, tmp_path):
        """Translation complete sets queued_at, doesn't modify created_at."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"TQ-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        task_before = _get_task(settings, gen["job_code"])
        original_created_at = task_before["created_at"]

        # Claim and complete
        claim_next_fast_translation(settings)
        time.sleep(0.1)
        ok = complete_fast_translation(
            settings,
            request_code=gen["request_code"],
            job_code=gen["job_code"],
            final_prompt="translated prompt",
            character_key="[]",
        )
        assert ok is True

        task_after = _get_task(settings, gen["job_code"])
        # created_at must not change
        assert task_after["created_at"] == original_created_at
        # queued_at must be set
        assert task_after["queued_at"] is not None
        assert task_after["queued_at"] >= original_created_at
        assert task_after["status"] == "queued"


# ============================================================
# I. Web restart recovery (simulated)
# ============================================================
class TestWebRestartRecovery:
    """Simulated web restart recovery scenario."""

    def test_stale_task_recovered_after_simulated_crash(self, tmp_path):
        """Task stuck in processing after crash → recovered on restart."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        gen = create_fast_translation_task_atomic(
            settings,
            job_code=f"WR-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="a sunset",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0,
            width=1024, height=1536, mode="txt2img",
            input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )

        # Claim (simulates worker picking it up)
        task = claim_next_fast_translation(settings)
        assert task is not None

        # Simulate crash: make stale
        conn = connect(settings)
        try:
            conn.execute(
                "UPDATE translation_requests SET started_at=?, attempt_count=1 WHERE request_code=?",
                (int(time.time()) - FAST_TRANSLATION_CLAIM_TTL - 10, gen["request_code"]),
            )
            conn.commit()
        finally:
            conn.close()

        # Simulate restart: run recovery
        recovered = recover_stale_fast_translation_tasks(settings)
        assert recovered == 1

        # Task should be requeued (attempt 1 < max 2)
        conn = connect(settings)
        try:
            tr = conn.execute(
                "SELECT status FROM translation_requests WHERE request_code=?",
                (gen["request_code"],),
            ).fetchone()
            assert tr["status"] == "queued"
        finally:
            conn.close()

        # Claim again and complete this time
        task2 = claim_next_fast_translation(settings)
        assert task2 is not None

        ok = complete_fast_translation(
            settings,
            request_code=gen["request_code"],
            job_code=gen["job_code"],
            final_prompt="1girl, sunset, outdoor",
            character_key="[]",
        )
        assert ok is True

        # Task should be queued for generation
        task_final = _get_task(settings, gen["job_code"])
        assert task_final["status"] == "queued"


# ============================================================
# J. Schema: queued_at column
# ============================================================
class TestSchemaQueuedAt:
    """queued_at column exists and handles correctly."""

    def test_queued_at_column_exists(self, tmp_path):
        """New database has queued_at column."""
        settings = _make_settings(tmp_path)
        conn = connect(settings)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()}
            assert "queued_at" in columns
        finally:
            conn.close()

    def test_queued_at_default_null(self, tmp_path):
        """New tasks have queued_at=NULL."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)

        result = create_task_atomic(
            settings,
            job_code=f"QA-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="test",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1536,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        task = _get_task(settings, result["job_code"])
        assert task["queued_at"] is None


# ============================================================
# K. Translation mode in generation_tasks
# ============================================================
class TestTranslationMode:
    """translation_mode is written to generation_tasks."""

    def test_normal_mode_written(self, tmp_path):
        """Normal task has translation_mode='none'."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)

        result = create_task_atomic(
            settings,
            job_code=f"TM-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            prompt="test",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1536,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
            translation_mode="none",
        )
        task = _get_task(settings, result["job_code"])
        assert task["translation_mode"] == "none"

    def test_fast_mode_written(self, tmp_path):
        """Fast task has translation_mode='fast'."""
        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)

        result = create_fast_translation_task_atomic(
            settings,
            job_code=f"TF-{uuid.uuid4().hex[:8].upper()}",
            user_id=TEST_USER_A,
            username="Test",
            original_prompt="test",
            translation_mode="fast",
            style_key="style_a",
            lora_weight=1.0, width=1024, height=1536,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt",
            auto_tagger=False,
        )
        task = _get_task(settings, result["job_code"])
        assert task["translation_mode"] == "fast"
