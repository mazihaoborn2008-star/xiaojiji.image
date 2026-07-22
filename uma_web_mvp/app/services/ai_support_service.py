from __future__ import annotations

import json
from typing import Any

from app.ai_support import repository
from app.ai_support.prompts import AI_SUPPORT_SYSTEM_PROMPT, SITE_FACTS
from app.ai_support.sanitize import sanitize_ai_reply, sanitize_user_message
from app.config import Settings
from app.services.deepseek_service import DeepSeekService
from app.services.task_view_service import NOT_FOUND_MESSAGE, extract_job_codes, get_owned_recent_tasks, get_owned_task_summary


class AiSupportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def ensure_enabled(settings: Settings) -> None:
    if not settings.ai_support_enabled:
        raise AiSupportError("ai_support_disabled", "AI 客服当前未启用")


def create_ai_support_conversation(settings: Settings, user_id: str) -> dict[str, Any]:
    ensure_enabled(settings)
    return repository.create_conversation(settings, user_id, title="AI Support")


def list_ai_support_conversations(settings: Settings, user_id: str) -> list[dict[str, Any]]:
    ensure_enabled(settings)
    return repository.list_conversations(settings, user_id)


def get_ai_support_conversation(settings: Settings, user_id: str, conversation_code: str) -> dict[str, Any]:
    ensure_enabled(settings)
    conversation = repository.get_conversation(settings, user_id, conversation_code)
    if not conversation:
        raise AiSupportError("not_found", "会话不存在")
    messages = repository.list_messages(settings, int(conversation["id"]))
    return {"conversation": conversation, "messages": messages}


def clear_ai_support_conversation(settings: Settings, user_id: str, conversation_code: str) -> None:
    ensure_enabled(settings)
    if not repository.clear_conversation(settings, user_id, conversation_code):
        raise AiSupportError("not_found", "会话不存在")


def _build_safe_context(settings: Settings, user_id: str, message: str) -> tuple[dict[str, Any], str]:
    codes = extract_job_codes(message)
    tasks: list[dict[str, Any]] = []
    referenced = ""
    if codes:
        for code in codes[:3]:
            task = get_owned_task_summary(settings, user_id, code)
            if task:
                tasks.append(task)
                referenced = task["job_code"]
            else:
                tasks.append({"job_code": code, "not_found": True, "message": NOT_FOUND_MESSAGE})
    else:
        tasks = get_owned_recent_tasks(settings, user_id, limit=3)
    context = {"site_facts": SITE_FACTS, "task_summaries": tasks}
    return context, referenced


async def send_ai_support_message(
    settings: Settings,
    *,
    user_id: str,
    conversation_code: str,
    message: str,
    deepseek: DeepSeekService | None = None,
) -> dict[str, Any]:
    ensure_enabled(settings)
    conversation = repository.get_conversation(settings, user_id, conversation_code)
    if not conversation:
        raise AiSupportError("not_found", "会话不存在")
    user_text = sanitize_user_message(message)
    if not user_text:
        raise AiSupportError("empty_message", "请输入问题")
    user_message = repository.add_message(settings, int(conversation["id"]), "user", user_text, status="done")
    context, referenced = _build_safe_context(settings, user_id, user_text)
    history = repository.list_recent_history(settings, int(conversation["id"]), settings.ai_support_max_history)
    prompt_payload = {
        "safe_context": context,
        "recent_history": history,
        "current_user_message": user_text,
        "output": "Reply to the user in their language. No JSON.",
    }
    ds = deepseek or DeepSeekService(settings)
    try:
        data = await ds.complete_json(
            system_prompt=AI_SUPPORT_SYSTEM_PROMPT,
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":")),
            temperature=0.2,
            max_tokens=settings.deepseek_chat_max_output_tokens,
            timeout_seconds=settings.deepseek_chat_timeout_seconds,
            purpose="ai_support",
            mock_response={"reply": _local_support_reply(user_text, context)},
        )
        reply_text = sanitize_ai_reply(str(data.get("reply") or data.get("answer") or ""))
    except Exception:
        reply_text = _local_support_reply(user_text, context)
    assistant_message = repository.add_message(
        settings,
        int(conversation["id"]),
        "assistant",
        reply_text,
        status="done",
        referenced_job_code=referenced,
    )
    return {
        "ok": True,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "safe_context": context,
    }


def _local_support_reply(user_text: str, context: dict[str, Any]) -> str:
    tasks = context.get("task_summaries") or []
    if tasks:
        task = tasks[0]
        if task.get("not_found"):
            return NOT_FOUND_MESSAGE
        return (
            f"任务 {task['job_code']} 当前状态是 {task['status']}，"
            f"扣费 {task['charged_credits']} credits，"
            f"{'已检测到退款记录' if task.get('refunded') else '暂未检测到退款记录'}。"
        )
    lowered = user_text.lower()
    if "退款" in user_text or "refund" in lowered:
        return "AI 客服不能直接退款。你可以前往畸形图退款页面提交申请，或通过反馈/消息中心联系管理员。"
    if "极速" in user_text or "fast" in lowered:
        return "极速翻译是单次 Prompt 整理流程，不进入聊天队列；普通翻译仍保留原来的翻译方式。"
    return "你好，我是小击击 AI 客服。你可以描述遇到的问题，也可以提供任务号让我帮你查询。"

