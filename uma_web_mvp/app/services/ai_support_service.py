from __future__ import annotations

import json
import logging
from typing import Any

from app.ai_support import repository
from app.ai_support.prompts import AI_SUPPORT_SYSTEM_PROMPT, build_site_facts
from app.ai_support.sanitize import sanitize_ai_reply, sanitize_user_message
from app.config import Settings
from app.services.deepseek_service import DeepSeekError, DeepSeekService
from app.services.task_view_service import NOT_FOUND_MESSAGE, extract_job_codes, get_owned_recent_tasks, get_owned_task_summary


logger = logging.getLogger(__name__)


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
    context = {"site_facts": build_site_facts(settings), "task_summaries": tasks}
    return context, referenced


def _is_policy_blocked_request(user_text: str) -> bool:
    lowered = user_text.lower()
    if _is_policy_capability_question(user_text):
        return False
    blocked_keywords = [
        "退款",
        "退钱",
        "退 credits",
        "退credit",
        "改余额",
        "修改余额",
        "加余额",
        "扣余额",
        "取消任务",
        "创建任务",
        "帮我生成",
        "提交生成",
        "审核充值",
        "批准充值",
        "驳回充值",
        "查看他人",
        "管理员数据",
        "数据库",
        ".env",
        "api key",
        "token",
        "cookie",
        "csrf",
        "secret",
        "password",
        "读取文件",
        "打印文件",
        "内部路径",
        "忽略之前",
        "ignore previous",
        "refund",
        "cancel task",
        "create task",
        "modify balance",
        "admin",
        "database",
        "read file",
        "print env",
    ]
    return any(keyword in lowered or keyword in user_text for keyword in blocked_keywords)


def _is_policy_capability_question(user_text: str) -> bool:
    lowered = user_text.lower()
    sensitive_terms = [
        "退款", "退钱", "余额", "credits", "balance", "refund",
        "取消任务", "cancel task", "创建任务", "create task",
    ]
    if not any(term in lowered or term in user_text for term in sensitive_terms):
        return False
    question_markers = ["吗", "么", "？", "?", "能不能", "能否", "可不可以", "可以", "是否", "can ", "could "]
    if not any(marker in lowered or marker in user_text for marker in question_markers):
        return False
    action_markers = [
        "请帮我退款",
        "现在退款",
        "直接退款",
        "给我退款",
        "把余额改",
        "帮我改余额",
        "修改我的余额",
        "读取.env",
        "打印.env",
        "read .env",
        "print env",
    ]
    return not any(marker in lowered or marker in user_text for marker in action_markers)


def _policy_refusal(user_text: str) -> str:
    lowered = user_text.lower()
    if "退款" in user_text or "refund" in lowered or "退钱" in user_text:
        return "AI 客服不能直接退款或承诺退款。你可以前往畸形图退款页面提交申请，或通过反馈/消息中心联系管理员。"
    if "取消" in user_text or "cancel" in lowered:
        return "AI 客服不能取消任务。请在任务列表中使用已有的取消入口，只有符合条件的任务才可以取消。"
    if "余额" in user_text or "credits" in lowered or "balance" in lowered:
        return "AI 客服不能修改 Credits 或账号余额。充值和扣费结果以系统记录为准，如有异常请提交反馈给管理员。"
    if "生成" in user_text or "create task" in lowered:
        return "AI 客服不能创建生图任务。请回到生图页面或 Smart Agent 页面确认提交。"
    return "这个请求涉及账号、系统或内部数据权限，AI 客服不能执行或查看。我可以解释公开功能、本人任务状态和页面操作。"


def _can_answer_locally(user_text: str, context: dict[str, Any]) -> bool:
    if extract_job_codes(user_text):
        return True
    return _is_policy_blocked_request(user_text)


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
        "output": 'Return JSON only as {"reply":"..."}; no code fences or extra keys.',
    }
    if _can_answer_locally(user_text, context):
        reply_text = _local_support_reply(user_text, context)
    else:
        if not str(settings.deepseek_api_key or "").strip():
            logger.warning("ai_support_deepseek_unavailable code=missing_api_key")
            raise AiSupportError("ai_support_unavailable", "AI 客服暂时不可用，请稍后再试")
        ds = deepseek or DeepSeekService(settings)
        try:
            data = await ds.complete_json(
                system_prompt=AI_SUPPORT_SYSTEM_PROMPT,
                user_prompt=json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":")),
                temperature=0.2,
                max_tokens=settings.deepseek_chat_max_output_tokens,
                timeout_seconds=settings.deepseek_chat_timeout_seconds,
                purpose="ai_support",
            )
            reply_text = sanitize_ai_reply(str(data.get("reply") or ""))
        except DeepSeekError as exc:
            logger.warning(
                "ai_support_deepseek_unavailable code=%s http_status=%s exception_type=%s",
                getattr(exc, "code", "deepseek_unavailable"),
                getattr(exc, "http_status", None),
                getattr(exc, "exception_type", None),
            )
            raise AiSupportError("ai_support_unavailable", "AI 客服暂时不可用，请稍后再试") from exc
        except Exception as exc:
            logger.warning("ai_support_deepseek_unavailable exception_type=%s", type(exc).__name__)
            raise AiSupportError("ai_support_unavailable", "AI 客服暂时不可用，请稍后再试") from exc
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
    }


def _local_support_reply(user_text: str, context: dict[str, Any]) -> str:
    if _is_policy_blocked_request(user_text):
        return _policy_refusal(user_text)
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

