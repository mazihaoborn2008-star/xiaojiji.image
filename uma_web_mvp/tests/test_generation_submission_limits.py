"""Generation Submission Limits Tests.

Tests for DB-authoritative rate limiting:
A. 10 active tasks per user
B. 20 submissions per 60 seconds
C. Rate limit counting (dedup, character confirm, different users)
D. Global queue limit
E. Insufficient credits
"""
from __future__ import annotations

import os
import sys
import time
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

TEST_USER_A = "test-user-alpha"
TEST_USER_B = "test-user-beta"


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
        "dev_username": "Limits Tester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "price_fen_per_image": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_dummy",
        "session_secret": "test-session-secret-for-limits-test-32chars!",
        "jwt_secret": "test-jwt-secret-for-limits-testing-32chars!",
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


def _seed_user(s: Settings, user_id: str, balance: int = 10000):
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


def _count_tasks(s: Settings, user_id: str) -> int:
    conn = connect(s)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ('smart_planning','translating','queued','processing')",
            (user_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _create_fast(s: Settings, user_id: str, prompt: str = "test", cid: str | None = None) -> dict:
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
        client_request_id=cid,
    )


# ────────────────────────────────────────────────────────────
# A. 10 active tasks per user
# ────────────────────────────────────────────────────────────

class TestActiveTaskLimit:
    def test_first_ten_allowed(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        for i in range(10):
            r = _create_fast(s, TEST_USER_A, f"prompt {i}")
            assert r["status"] == "translating"

        assert _count_tasks(s, TEST_USER_A) == 10

    def test_eleventh_rejected_no_charge(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        for i in range(10):
            _create_fast(s, TEST_USER_A, f"prompt {i}")

        balance_after_10 = _get_balance(s, TEST_USER_A)

        with pytest.raises(RuntimeError, match="active_task_limit"):
            _create_fast(s, TEST_USER_A, "one too many")

        assert _get_balance(s, TEST_USER_A) == balance_after_10
        assert _count_tasks(s, TEST_USER_A) == 10

    def test_cancel_releases_slot(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=3)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        r1 = _create_fast(s, TEST_USER_A, "p1")
        _create_fast(s, TEST_USER_A, "p2")
        _create_fast(s, TEST_USER_A, "p3")

        cancel_task_atomic(s, TEST_USER_A, r1["job_code"])
        assert _count_tasks(s, TEST_USER_A) == 2

        r4 = _create_fast(s, TEST_USER_A, "p4")
        assert r4["status"] == "translating"
        assert _count_tasks(s, TEST_USER_A) == 3

    def test_terminal_tasks_not_counted(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=2)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        r1 = _create_fast(s, TEST_USER_A, "p1")
        _create_fast(s, TEST_USER_A, "p2")

        # Cancel first
        cancel_task_atomic(s, TEST_USER_A, r1["job_code"])

        # Can create again (terminal not counted)
        r3 = _create_fast(s, TEST_USER_A, "p3")
        assert r3["status"] == "translating"


# ────────────────────────────────────────────────────────────
# B. 20 submissions per 60 seconds
# ────────────────────────────────────────────────────────────

class TestSubmissionRateLimit:
    def test_first_twenty_allowed(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=100)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        for i in range(20):
            r = _create_fast(s, TEST_USER_A, f"prompt {i}", cid=f"rate-{i}")
            assert r["status"] == "translating"

    def test_twenty_first_rejected(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=100)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        for i in range(20):
            _create_fast(s, TEST_USER_A, f"prompt {i}", cid=f"rate-{i}")

        balance_after_20 = _get_balance(s, TEST_USER_A)

        with pytest.raises(RuntimeError, match="generation_rate_limited"):
            _create_fast(s, TEST_USER_A, "the 21st", cid="rate-21")

        assert _get_balance(s, TEST_USER_A) == balance_after_20

    def test_same_client_id_retry_not_counted(self, tmp_path):
        """Same client_request_id with same fingerprint returns deduped, doesn't count."""
        s = _make_settings(tmp_path, max_active_tasks_per_user=100)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        cid = "dedup-test-001"
        r1 = _create_fast(s, TEST_USER_A, "same prompt", cid=cid)
        r2 = _create_fast(s, TEST_USER_A, "same prompt", cid=cid)

        assert r2["deduped"] is True
        assert r1["job_code"] == r2["job_code"]

    def test_different_users_independent(self, tmp_path):
        s = _make_settings(tmp_path, max_active_tasks_per_user=100)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)
        _seed_user(s, TEST_USER_B)

        for i in range(20):
            _create_fast(s, TEST_USER_A, f"a-{i}", cid=f"a-{i}")

        # User B can still create
        r = _create_fast(s, TEST_USER_B, "b-0", cid="b-0")
        assert r["status"] == "translating"


# ────────────────────────────────────────────────────────────
# C. Insufficient credits
# ────────────────────────────────────────────────────────────

class TestInsufficientCredits:
    def test_rejected_when_balance_too_low(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 1)  # Only 1 credit, need 3

        with pytest.raises(RuntimeError, match="insufficient_credits"):
            _create_fast(s, TEST_USER_A)

        # Balance unchanged
        assert _get_balance(s, TEST_USER_A) == 1

    def test_no_task_created_on_insufficient(self, tmp_path):
        s = _make_settings(tmp_path)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A, 1)

        with pytest.raises(RuntimeError):
            _create_fast(s, TEST_USER_A)

        assert _count_tasks(s, TEST_USER_A) == 0


# ────────────────────────────────────────────────────────────
# D. Global queue limit
# ────────────────────────────────────────────────────────────

class TestGlobalQueueLimit:
    def test_global_limit_rejected(self, tmp_path):
        s = _make_settings(tmp_path, max_queue_size=5, max_active_tasks_per_user=100)
        ensure_schema(s)
        _seed_user(s, TEST_USER_A)

        for i in range(5):
            _create_fast(s, TEST_USER_A, f"p-{i}", cid=f"g-{i}")

        balance_after = _get_balance(s, TEST_USER_A)

        with pytest.raises(RuntimeError, match="queue_full"):
            _create_fast(s, TEST_USER_A, "too many", cid="g-5")

        assert _get_balance(s, TEST_USER_A) == balance_after
