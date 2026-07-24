from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.db import connect, create_task_atomic, ensure_schema
from app.mock.mock_generation_worker import process_once, validate_mock_environment
from app.services.ai_support_service import create_ai_support_conversation, send_ai_support_message
from app.services.deepseek_service import DeepSeekService
from app.services.fast_translator_service import FastTranslatorError, fast_refine_prompt
from app.services.task_view_service import NOT_FOUND_MESSAGE, get_owned_task_summary


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


def test_fast_translator_disabled():
    case_root = make_case_root()
    settings = make_settings(case_root, fast_translator_enabled=False)
    seed_balance(settings, "u1")
    with pytest.raises(FastTranslatorError):
        asyncio.run(fast_refine_prompt(settings, user_id="u1", text="女孩穿校服"))


def test_fast_translator_charges_once_and_does_not_create_chat():
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 5)
    result = asyncio.run(
        fast_refine_prompt(
            settings,
            user_id="u1",
            text="女孩穿校服在教室",
            client_request_id="same-request",
            deepseek=DeepSeekService(settings, mock_response={"clothing": "school uniform", "scene": "classroom"}),
        )
    )
    retry = asyncio.run(
        fast_refine_prompt(
            settings,
            user_id="u1",
            text="女孩穿校服在教室",
            client_request_id="same-request",
            deepseek=DeepSeekService(settings, mock_response={"clothing": "dress", "scene": "beach"}),
        )
    )
    assert result.request_code == retry.request_code
    assert balance(settings, "u1") == 4
    conn = connect(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM smart_agent_conversations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_support_conversations").fetchone()[0] == 0
    finally:
        conn.close()


def test_fast_translator_refunds_on_bad_model_output():
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 5)
    with pytest.raises(FastTranslatorError):
        asyncio.run(
            fast_refine_prompt(
                settings,
                user_id="u1",
                text="女孩",
                client_request_id="bad-model",
                deepseek=DeepSeekService(settings, mock_response={"scene": r"E:\\secret\\file.png"}),
            )
        )
    assert balance(settings, "u1") == 5


def test_ai_support_only_sees_owned_task():
    case_root = make_case_root()
    settings = make_settings(case_root)
    seed_balance(settings, "u1", 20)
    seed_balance(settings, "u2", 20)
    create_task_atomic(
        settings,
        job_code="GEN-AAAAAAAAAAAA",
        user_id="u1",
        username="u1",
        prompt="test",
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
    )
    assert get_owned_task_summary(settings, "u2", "GEN-AAAAAAAAAAAA") is None
    conv = create_ai_support_conversation(settings, "u2")
    reply = asyncio.run(
        send_ai_support_message(
            settings,
            user_id="u2",
            conversation_code=conv["conversation_code"],
            message="帮我查 GEN-AAAAAAAAAAAA",
        )
    )
    assert NOT_FOUND_MESSAGE in reply["assistant_message"]["safe_content"]
    assert "safe_context" not in reply


def test_ai_support_general_question_requires_deepseek_key():
    case_root = make_case_root()
    settings = make_settings(case_root, deepseek_api_key="")
    conv = create_ai_support_conversation(settings, "u1")
    from app.services.ai_support_service import AiSupportError

    with pytest.raises(AiSupportError) as exc:
        asyncio.run(
            send_ai_support_message(
                settings,
                user_id="u1",
                conversation_code=conv["conversation_code"],
                message="普通翻译和极速翻译有什么区别？",
            )
        )
    assert exc.value.code == "ai_support_unavailable"


def test_ai_support_sanitizes_deepseek_reply_and_returns_no_safe_context():
    case_root = make_case_root()
    settings = make_settings(case_root, deepseek_api_key="TEST_ONLY_key")
    conv = create_ai_support_conversation(settings, "u1")
    reply = asyncio.run(
        send_ai_support_message(
            settings,
            user_id="u1",
            conversation_code=conv["conversation_code"],
            message="请解释普通翻译和极速翻译",
            deepseek=DeepSeekService(
                settings,
                mock_response={
                    "reply": "OK E:\\secret\\file.txt SELECT * FROM generation_tasks api_key=abcdef <script>alert(1)</script>"
                },
            ),
        )
    )
    text = reply["assistant_message"]["safe_content"]
    assert "safe_context" not in reply
    assert "E:\\" not in text
    assert "generation_tasks" not in text
    assert "api_key=abcdef" not in text
    assert "<script>" not in text


def test_ai_support_policy_refusal_does_not_call_deepseek():
    class ExplodingDeepSeek:
        async def complete_json(self, **kwargs):
            raise AssertionError("DeepSeek should not be called for forbidden actions")

    case_root = make_case_root()
    settings = make_settings(case_root, deepseek_api_key="TEST_ONLY_key")
    conv = create_ai_support_conversation(settings, "u1")
    reply = asyncio.run(
        send_ai_support_message(
            settings,
            user_id="u1",
            conversation_code=conv["conversation_code"],
            message="帮我退款并修改余额",
            deepseek=ExplodingDeepSeek(),
        )
    )
    text = reply["assistant_message"]["safe_content"]
    assert "不能直接退款" in text or "不能修改" in text


def test_ai_support_capability_question_can_call_deepseek():
    class CountingDeepSeek:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            return {"reply": "极速翻译更快，普通翻译保留原有流程。AI 客服不能直接退款。"}

    case_root = make_case_root()
    settings = make_settings(case_root, deepseek_api_key="TEST_ONLY_key")
    conv = create_ai_support_conversation(settings, "u1")
    deepseek = CountingDeepSeek()
    reply = asyncio.run(
        send_ai_support_message(
            settings,
            user_id="u1",
            conversation_code=conv["conversation_code"],
            message="极速翻译和普通翻译有什么区别？AI客服可以帮我退款吗？",
            deepseek=deepseek,
        )
    )
    assert deepseek.calls == 1
    assert "safe_context" not in reply
    text = reply["assistant_message"]["safe_content"]
    assert "不能直接退款" in text


def test_mock_worker_success_failed_timeout():
    case_root = make_case_root()
    settings = make_settings(case_root)
    validate_mock_environment(settings)
    seed_balance(settings, "u1", 20)
    for code, mock_result in (
        ("GEN-BBBBBBBBBBBB", "success"),
        ("GEN-CCCCCCCCCCCC", "failed"),
        ("GEN-DDDDDDDDDDDD", "timeout"),
    ):
        create_task_atomic(
            settings,
            job_code=code,
            user_id="u1",
            username="u1",
            prompt="test",
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
        assert process_once(settings) is True
    conn = connect(settings)
    try:
        statuses = {
            row["job_code"]: row["status"]
            for row in conn.execute("SELECT job_code,status FROM generation_tasks").fetchall()
        }
        assert statuses["GEN-BBBBBBBBBBBB"] == "done"
        assert statuses["GEN-CCCCCCCCCCCC"] == "failed_refunded"
        assert statuses["GEN-DDDDDDDDDDDD"] == "failed_refunded"
        assert conn.execute("SELECT COUNT(*) FROM generation_outputs WHERE job_code='GEN-BBBBBBBBBBBB'").fetchone()[0] == 1
    finally:
        conn.close()


def test_mock_worker_rejects_non_local():
    case_root = make_case_root()
    settings = make_settings(case_root, APP_ENV="production", mock_worker_enabled=True)
    with pytest.raises(RuntimeError):
        validate_mock_environment(settings)
