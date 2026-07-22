"""Tests for mock worker state handling, claim safety, and mock_result isolation."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.db import connect, create_task_atomic, ensure_schema
from app.mock.mock_generation_worker import (
    _complete_failed_with_refund,
    _mock_result_for,
    claim_one,
    complete_failed,
    complete_success,
    leave_timeout,
    process_once,
    validate_mock_environment,
)
from app.services.deepseek_service import DeepSeekService

TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "pytest_cases"


def make_case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_settings(case_root: Path, **overrides) -> Settings:
    test_root = case_root / "test_data"
    output = test_root / "output"
    mock_output = test_root / "mock_output"
    input_images = test_root / "input_images"
    for path in (output, mock_output, input_images):
        path.mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:8001",
        "BALANCE_DB": str(test_root / "local_test.db"),
        "BOT_OUTPUT_DIR": str(output),
        "mock_output_dir": str(mock_output),
        "INPUT_IMAGE_DIR": str(input_images),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": "local-user",
        "owner_free_generation": False,
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 1,
        "ai_support_enabled": True,
        "mock_worker_enabled": True,
        "mock_generation_seconds": 0,
        "deepseek_api_key": "",
    }
    data.update(overrides)
    settings = Settings(**data)
    settings.validate_local_isolation()
    ensure_schema(settings)
    return settings


def seed_balance(settings: Settings, user_id: str, amount: int = 20) -> None:
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id,balance_fen) VALUES (?,?)", (user_id, amount))
        conn.commit()
    finally:
        conn.close()


def balance(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance_fen"] if row else 0)
    finally:
        conn.close()


def get_task_status(settings: Settings, job_code: str) -> str:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        return str(row["status"]) if row else ""
    finally:
        conn.close()


def get_task_error_code(settings: Settings, job_code: str) -> str:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT error_code FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        return str(row["error_code"] or "") if row else ""
    finally:
        conn.close()


def get_task_finished_at(settings: Settings, job_code: str) -> int | None:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT finished_at FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        val = row["finished_at"] if row else None
        return int(val) if val else None
    finally:
        conn.close()


def count_ledger_refunds(settings: Settings, job_code: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM balance_ledger WHERE order_code=? AND reason='mock_generation_refund'",
            (job_code,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def count_outputs(settings: Settings, job_code: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM generation_outputs WHERE job_code=?", (job_code,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def create_test_task(settings: Settings, job_code: str, user_id: str = "u1", mock_result: str = "") -> None:
    create_task_atomic(
        settings,
        job_code=job_code,
        user_id=user_id,
        username=user_id,
        prompt="test prompt",
        style_key="style_a",
        lora_weight=1,
        width=1024,
        height=1536,
        mode="txt2img",
        input_image_path=None,
        denoise=0.5,
        control_type="depth",
        control_character="prompt",
        auto_tagger=False,
        mock_result=mock_result,
    )


# ── timeout 最终状态 ──


def test_timeout_task_enters_failed_refunded():
    """timeout 后任务不再是 processing，而是 failed_refunded"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-TIMEOUTAAAA", "u1", mock_result="timeout")
    assert process_once(settings) is True
    assert get_task_status(settings, "GEN-TIMEOUTAAAA") == "failed_refunded"
    assert get_task_error_code(settings, "GEN-TIMEOUTAAAA") == "mock_timeout"
    assert get_task_finished_at(settings, "GEN-TIMEOUTAAAA") is not None


def test_timeout_releases_active_task():
    """timeout 后任务不再计入 active task"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-TIMEOUTAAAA", "u1", mock_result="timeout")
    process_once(settings)
    conn = connect(settings)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE status IN ('processing','queued') AND user_id='u1'"
        ).fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_timeout_credits_refunded_once():
    """timeout 后 credits 只退款一次"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-TIMEOUTAAAA", "u1", mock_result="timeout")
    assert balance(settings, "u1") == 9
    process_once(settings)
    assert balance(settings, "u1") == 10
    assert count_ledger_refunds(settings, "GEN-TIMEOUTAAAA") == 1


def test_timeout_idempotent_repeat_call():
    """timeout 处理函数重复调用不会重复退款"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-TIMEOUTAAAA", "u1", mock_result="timeout")
    row = claim_one(settings)
    assert row is not None
    leave_timeout(settings, row)
    leave_timeout(settings, row)  # second call
    assert balance(settings, "u1") == 10
    assert count_ledger_refunds(settings, "GEN-TIMEOUTAAAA") == 1


def test_timeout_task_not_reclaimed():
    """timeout 任务不会被再次 claim"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-TIMEOUTAAAA", "u1", mock_result="timeout")
    process_once(settings)
    assert claim_one(settings) is None


# ── claim 安全 ──


def test_claim_one_returns_none_when_nothing_queued():
    """没有 queued 任务时返回 None"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    assert claim_one(settings) is None


def test_claim_one_returns_task_and_updates_status():
    """claim 成功后任务变为 processing"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-CLAIMAAAAAA", "u1")
    row = claim_one(settings)
    assert row is not None
    assert row["job_code"] == "GEN-CLAIMAAAAAA"
    assert get_task_status(settings, "GEN-CLAIMAAAAAA") == "processing"


def test_claim_one_skips_already_processing():
    """已 processing 的任务不会被再次 claim"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-CLAIMAAAAAA", "u1")
    row1 = claim_one(settings)
    assert row1 is not None
    row2 = claim_one(settings)
    assert row2 is None


def test_completed_task_not_reclaimed():
    """completed 任务不会被再次处理"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-DONEAAAAAAA", "u1", mock_result="success")
    process_once(settings)
    assert get_task_status(settings, "GEN-DONEAAAAAAA") == "done"
    assert claim_one(settings) is None


def test_failed_task_not_reclaimed():
    """failed 任务不会被再次处理"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-FAILEDBBBBB", "u1", mock_result="failed")
    process_once(settings)
    assert get_task_status(settings, "GEN-FAILEDBBBBB") == "failed_refunded"
    assert claim_one(settings) is None


def test_concurrent_claim_only_one_succeeds():
    """两个并发 claim 只有一个成功"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-CONCURENTCCCC", "u1")
    row1 = claim_one(settings)
    row2 = claim_one(settings)
    assert (row1 is not None) != (row2 is not None) or (row1 is None and row2 is None)
    assert row1 is not None
    assert row2 is None


# ── PIL 失败处理 ──


def test_pil_failure_enters_failed_refunded():
    """PIL 保存异常后任务进入 failed_refunded"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-PILFAILDDDD", "u1", mock_result="success")
    row = claim_one(settings)
    assert row is not None
    original_path = settings.mock_output_path

    class BadSettings:
        """Triggers PIL failure by pointing to invalid output dir."""
        mock_output_path = Path("/nonexistent/path/that/does/not/exist/at/all")

    from unittest.mock import patch
    with patch("app.mock.mock_generation_worker._create_placeholder", side_effect=OSError("disk full")):
        complete_success(settings, row)
    assert get_task_status(settings, "GEN-PILFAILDDDD") == "failed_refunded"
    assert get_task_error_code(settings, "GEN-PILFAILDDDD") == "mock_output_failed"


def test_pil_failure_refunds_once():
    """PIL 保存异常后只退款一次"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-PILFAILDDDD", "u1", mock_result="success")
    assert balance(settings, "u1") == 9
    row = claim_one(settings)
    from unittest.mock import patch
    with patch("app.mock.mock_generation_worker._create_placeholder", side_effect=OSError("disk full")):
        complete_success(settings, row)
    assert balance(settings, "u1") == 10
    assert count_ledger_refunds(settings, "GEN-PILFAILDDDD") == 1


def test_pil_failure_no_invalid_outputs():
    """PIL 保存异常后不写无效 generation_outputs"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-PILFAILDDDD", "u1", mock_result="success")
    row = claim_one(settings)
    from unittest.mock import patch
    with patch("app.mock.mock_generation_worker._create_placeholder", side_effect=OSError("disk full")):
        complete_success(settings, row)
    assert count_outputs(settings, "GEN-PILFAILDDDD") == 0


# ── mock_result 隔离 ──


def test_mock_result_allowed_in_local():
    """local + 合法值允许保存 mock_result"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    for val in ("success", "failed", "timeout"):
        code = f"GEN-MOCK{val.upper()[:5].ljust(5, 'X')}"
        create_test_task(settings, code, "u1", mock_result=val)
    conn = connect(settings)
    try:
        rows = conn.execute("SELECT job_code, mock_result FROM generation_tasks ORDER BY job_code").fetchall()
        for row in rows:
            assert row["mock_result"] in {"success", "failed", "timeout"}
    finally:
        conn.close()


def test_mock_result_illegal_value_rejected():
    """local + 非法值被拒绝"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    with pytest.raises(RuntimeError, match="invalid mock_result"):
        create_test_task(settings, "GEN-ILLEGALVALU", "u1", mock_result="drop_table")


def test_mock_result_empty_allowed():
    """空 mock_result 允许"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-EMPTYMOCKXX", "u1", mock_result="")
    assert get_task_status(settings, "GEN-EMPTYMOCKXX") == "queued"


def test_mock_result_forbidden_in_production():
    """非 local 环境禁止保存 mock_result"""
    case_root = make_case_root()
    settings = make_settings(case_root, APP_ENV="production")
    seed_balance(settings, "u1", 10)
    with pytest.raises(RuntimeError, match="mock_result is only allowed"):
        create_test_task(settings, "GEN-PRODMOCKXXX", "u1", mock_result="success")


def test_mock_result_forbidden_when_worker_disabled():
    """mock_worker_enabled=false 时禁止保存 mock_result"""
    case_root = make_case_root()
    settings = make_settings(case_root, mock_worker_enabled=False)
    seed_balance(settings, "u1", 10)
    with pytest.raises(RuntimeError, match="mock_result is only allowed"):
        create_test_task(settings, "GEN-NOWORKERXXX", "u1", mock_result="success")


def test_normal_task_without_mock_result_works():
    """普通任务没有 mock_result 时仍正常"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-NORMALXXXXX", "u1")
    assert get_task_status(settings, "GEN-NORMALXXXXX") == "queued"


def test_mock_result_for_prompt_fallback():
    """prompt 中的 [mock:failed] 标记仍能工作"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    row = {"prompt": "something [mock:failed] here", "mock_result": ""}
    assert _mock_result_for(row) == "failed"


def test_mock_result_for_explicit_overrides_prompt():
    """明确的 mock_result 优先于 prompt 标记"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    row = {"prompt": "something [mock:failed] here", "mock_result": "success"}
    assert _mock_result_for(row) == "success"


# ── _complete_failed_with_refund 幂等 ──


def test_complete_failed_with_refund_idempotent():
    """重复调用 _complete_failed_with_refund 不会重复退款"""
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 10)
    create_test_task(settings, "GEN-IDEMPOTENTX", "u1", mock_result="failed")
    row = claim_one(settings)
    assert row is not None
    complete_failed(settings, row)
    assert balance(settings, "u1") == 10
    assert count_ledger_refunds(settings, "GEN-IDEMPOTENTX") == 1
    # Second call should be no-op
    _complete_failed_with_refund(
        settings,
        job_code="GEN-IDEMPOTENTX",
        user_id="u1",
        charged=1,
        error="LOCAL MOCK failed",
        error_code="mock_failed",
    )
    assert balance(settings, "u1") == 10
    assert count_ledger_refunds(settings, "GEN-IDEMPOTENTX") == 1
