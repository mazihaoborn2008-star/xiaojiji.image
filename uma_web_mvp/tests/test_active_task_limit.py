from __future__ import annotations

import concurrent.futures
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.db import connect, create_fast_translation_task_atomic, create_task_atomic, ensure_schema


TEST_USER = "active-limit-user"


def _make_settings(tmp_path, **overrides):
    db_path = tmp_path / "test.db"
    input_dir = tmp_path / "input_images"
    output_dir = tmp_path / "output"
    mock_dir = tmp_path / "mock_output"
    for path in (input_dir, output_dir, mock_dir):
        path.mkdir(parents=True, exist_ok=True)
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
        "dev_username": "Active Limit Tester",
        "session_secret": "test-session-secret-active-limit-32chars",
        "jwt_secret": "test-jwt-secret-active-limit-32chars",
        "mock_worker_enabled": True,
        "fast_translator_enabled": True,
        "deepseek_api_key": "TEST_ONLY_key",
        "deepseek_model": "test-model",
        "price_fen_per_image": 1,
        "agent_surcharge_credits": 1,
        "fast_translator_cost_credits": 2,
        "max_active_tasks_per_user": 10,
        # These intentionally sit below the active-task limit to verify that
        # submit/queue throttles do not masquerade as the 10-active-task gate.
        "generation_submit_user_limit": 8,
        "max_queue_size": 8,
    }
    defaults.update(overrides)
    settings = Settings(**defaults)
    ensure_schema(settings)
    return settings


def _seed_balance(settings, amount=1000, user_id=TEST_USER):
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (user_id, amount))
        conn.commit()
    finally:
        conn.close()


def _balance(settings, user_id=TEST_USER):
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _task_count(settings, user_id=TEST_USER):
    conn = connect(settings)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM generation_tasks WHERE user_id=?", (user_id,)).fetchone()[0])
    finally:
        conn.close()


def _translation_count(settings, user_id=TEST_USER):
    conn = connect(settings)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM translation_requests WHERE user_id=?", (user_id,)).fetchone()[0])
    finally:
        conn.close()


def _create_none(settings, idx, user_id=TEST_USER):
    return create_task_atomic(
        settings,
        job_code=f"NL-{idx}-{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        username="Tester",
        prompt=f"test prompt {idx}",
        style_key="style_a",
        lora_weight=1.0,
        width=1024,
        height=1024,
        mode="txt2img",
        input_image_path=None,
        denoise=0.5,
        control_type="depth",
        control_character="",
        auto_tagger=False,
        use_agent=False,
        client_request_id=f"none-{idx}-{uuid.uuid4().hex[:8]}",
        translation_mode="none",
        request_fingerprint=f"none-fp-{idx}-{uuid.uuid4().hex[:8]}",
    )


def _create_normal(settings, idx, user_id=TEST_USER):
    return create_task_atomic(
        settings,
        job_code=f"NM-{idx}-{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        username="Tester",
        prompt=f"normal agent prompt {idx}",
        style_key="style_a",
        lora_weight=1.0,
        width=1024,
        height=1024,
        mode="txt2img",
        input_image_path=None,
        denoise=0.5,
        control_type="depth",
        control_character="",
        auto_tagger=False,
        use_agent=True,
        client_request_id=f"normal-{idx}-{uuid.uuid4().hex[:8]}",
        translation_mode="normal",
        request_fingerprint=f"normal-fp-{idx}-{uuid.uuid4().hex[:8]}",
    )


def _create_fast(settings, idx, user_id=TEST_USER):
    return create_fast_translation_task_atomic(
        settings,
        job_code=f"FT-{idx}-{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        username="Tester",
        original_prompt=f"fast prompt {idx}",
        translation_mode="fast",
        style_key="style_a",
        lora_weight=1.0,
        width=1024,
        height=1024,
        mode="txt2img",
        input_image_path=None,
        denoise=0.5,
        control_type="depth",
        control_character="",
        auto_tagger=False,
        character_keys=[],
        client_request_id=f"fast-{idx}-{uuid.uuid4().hex[:8]}",
    )


def _set_status(settings, job_code, status):
    conn = connect(settings)
    try:
        conn.execute("UPDATE generation_tasks SET status=? WHERE job_code=?", (status, job_code))
        conn.commit()
    finally:
        conn.close()


def test_none_normal_fast_share_ten_active_task_limit(tmp_path):
    settings = _make_settings(tmp_path)
    _seed_balance(settings, 1000)

    created = []
    for idx in range(4):
        created.append(_create_none(settings, idx))
    for idx in range(3):
        created.append(_create_normal(settings, idx))
    for idx in range(3):
        created.append(_create_fast(settings, idx))

    assert len(created) == 10
    assert _task_count(settings) == 10
    assert _translation_count(settings) == 3
    balance_after_ten = _balance(settings)

    try:
        _create_fast(settings, 99)
        assert False, "11th active task should be rejected"
    except RuntimeError as exc:
        assert str(exc) == "too_many_active_tasks"

    assert _task_count(settings) == 10
    assert _translation_count(settings) == 3
    assert _balance(settings) == balance_after_ten

    _set_status(settings, created[0]["job_code"], "done")
    replacement = _create_fast(settings, 100)
    assert replacement["status"] == "translating"
    assert _task_count(settings) == 11
    assert _translation_count(settings) == 4


def test_terminal_statuses_release_active_slots(tmp_path):
    terminal_statuses = ["done", "failed_refunded", "cancelled", "cancelled_refunded", "failed"]
    for status in terminal_statuses:
        settings = _make_settings(tmp_path / status)
        _seed_balance(settings, 1000)
        created = [_create_none(settings, idx) for idx in range(10)]
        _set_status(settings, created[0]["job_code"], status)
        replacement = _create_none(settings, 100)
        assert replacement["status"] == "queued"


def test_pending_and_smart_planning_count_as_active(tmp_path):
    settings = _make_settings(tmp_path)
    _seed_balance(settings, 1000)
    created = [_create_none(settings, idx) for idx in range(10)]
    for job, status in zip(created, ["pending", "smart_planning", "queued", "translating", "processing"] * 2):
        _set_status(settings, job["job_code"], status)

    try:
        _create_none(settings, 99)
        assert False, "pending/smart_planning/queued/translating/processing should count as active"
    except RuntimeError as exc:
        assert str(exc) == "too_many_active_tasks"


def test_concurrent_eleventh_request_does_not_create_or_charge(tmp_path):
    settings = _make_settings(tmp_path, generation_submit_user_limit=50, max_queue_size=50)
    _seed_balance(settings, 1000)

    def attempt(idx):
        try:
            return ("ok", _create_none(settings, idx))
        except RuntimeError as exc:
            return ("error", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as executor:
        results = list(executor.map(attempt, range(11)))

    ok_count = sum(1 for status, _ in results if status == "ok")
    errors = [payload for status, payload in results if status == "error"]
    assert ok_count == 10
    assert errors == ["too_many_active_tasks"]
    assert _task_count(settings) == 10
    assert _balance(settings) == 990
