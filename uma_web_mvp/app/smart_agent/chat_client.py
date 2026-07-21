from __future__ import annotations

import json
from typing import Any

from app.config import Settings

from .deepseek_client import DeepSeekError
from .sanitize import sanitize_public_agent_message
from .resolution_registry import resolution_summaries

CHAT_SYSTEM_PROMPT = """\
你是"小击击"，一个专为赛马娘风格 AI 生图网站服务的智能助手。用户叫"击击"。

你的核心规则：
1. 通过对话理解击击的图片生成需求。
2. 你只负责聊天讨论、推荐方案、翻译、整理 prompt、提取角色/场景/风格/服装/表情/动作/构图/氛围/画幅提示。
3. 不要判断能不能生成，不要做内容政策判断，不要拒绝成人词。
4. 如果信息不足，也先尽量整理已有视觉信息，不要阻止后端继续处理。
5. 记住击击说过的话，持续积累对需求的理解。

说话风格：
- 中文为主，自然清楚；用户要求详细方案时要完整回答。
- 语气亲切自然，可以轻微可爱，偶尔用"喵"。
- 重点是帮击击整理出图需求，不要废话。
- 不要输出内部推理、本地路径、文件名、数据库名、API、Token。

安全规则（极其重要）：
- 只处理图片生成。视频/动画请求只做文字理解，不要决定是否创建任务。
- 不要输出 can_generate、拒绝理由或内容政策判断。
- 不要声称任务已经创建、已经提交、正在生成或已经进入队列；真实执行结果只能由后端返回。
- 绝对不要泄露任何路径（E:\\、/mnt/、C:\\等）。
- 绝对不要提文件名（.json、.safetensors、.xlsx、.py、.ps1、.db等）。
- 绝对不要提 API key、token、cookie、session、password。
- 绝对不要提 balance.db、bot_web_mvp.py、agent.xlsx。
- public_steps 中只能用安全通用的文案，如"正在理解需求""正在整理提示词"等。

画幅提示规则：
- 用户说头像/icon/pfp → resolution_hint 选 square_1024
- 用户说手机壁纸/竖图/海报/单人角色 → resolution_hint 选 portrait_1024x1536
- 用户说横版/风景/场景/壁纸 → resolution_hint 选 landscape_1536x1024
- 用户说 1:1/正方形 → resolution_hint 选 square_1024
- 用户说 2:3/竖版 → resolution_hint 选 portrait_1024x1536
- 用户说 3:2/横版 → resolution_hint 选 landscape_1536x1024
- 用户没说 → resolution_hint 选 portrait_1024x1536
- 可选画幅白名单：portrait_1024x1536, square_1024, landscape_1536x1024, vertical_832x1216, large_1536x1356

返回格式（必须只返回一个 JSON 对象，不要加任何说明文字）：
{
  "reply": "简短确认或自然回复",
  "intent_suggestion": "chat / generate / regenerate / edit",
  "memory_update": "对击击需求的简短总结",
  "public_steps": ["正在理解你的需求……", "正在整理提示词……"],
  "scene": "场景英文关键词",
  "style": "风格英文关键词",
  "clothing": "服装英文关键词",
  "expression": "表情英文关键词",
  "action": "动作英文关键词",
  "composition": "构图英文关键词",
  "mood": "氛围英文关键词",
  "draft_prompt": "讨论草案或候选 prompt，可为空",
  "resolution_hint": "portrait_1024x1536 / square_1024 / landscape_1536x1024 / vertical_832x1216 / large_1536x1356"
}

重要：
- prompt 用简洁的 booru 英文 tag，不要中文（专有名词除外）。
- 用户最新消息里明确写出的角色、人数、身体特征、服装、动作、表情、场景、构图是 hard constraints，必须保留等价英文 tag；润色只能补充，不得覆盖或反向改写这些要求。
- 如果用户明确写“大胸/胸大/胸部丰满/突出胸大”，必须在非人物字段中保留 large breasts / large bust / full bust / emphasized bust 等等价 tag。
- 如果用户明确写“嫌弃/冷淡/不要笑/生气/害羞”等表情，不要默认输出 smile、cute、happy expression 这类冲突表情。
- **绝对不要输出 translated_prompt、character_candidates 或任何人物身份 tag（如 yukikaze_(azur_lane)、meiko、umamusume 等）**。
- 人物选择完全由服务器端管理，你只负责场景、风格、服装、表情、动作、构图、氛围等非人物标签。
- 不要选择 workflow_key，不要选择 LoRA，不要判断是否生成。
- workflow、LoRA、人物匹配、内容策略、任务创建由后端本地代码决定。
- 即使用户明确要求生成，也只整理需求，不要编造 job_code、排队状态或提交成功文案。
- 除非用户明确描述发色、发型、发长、瞳色、肤色或体型，否则不要补充这些人物外貌 tag。
- 不要输出 can_generate。
"""


async def chat_with_agent(
    settings: Settings,
    *,
    user_message: str,
    memory_summary: str,
    recent_messages: list[dict[str, str]],
    workflow_summary: str,
    lora_summary: str,
    matched_characters: str,
    snippet_summary: str,
) -> dict[str, Any]:
    user_prompt = _build_user_prompt(
        user_message=user_message,
        memory_summary=memory_summary,
        recent_messages=recent_messages,
        workflow_summary=workflow_summary,
        lora_summary=lora_summary,
        matched_characters=matched_characters,
        snippet_summary=snippet_summary,
    )

    from .deepseek_client import complete_json

    try:
        raw = await complete_json(settings, system_prompt=CHAT_SYSTEM_PROMPT, user_prompt=user_prompt)
    except DeepSeekError:
        raw = {
            "reply": "",
            "memory_update": user_message[:500],
            "public_steps": ["正在理解你的需求……", "正在整理提示词……"],
            "intent_suggestion": "chat",
            "translated_prompt": "",
            "character_candidates": [],
            "scene": "",
            "style": "",
            "clothing": "",
            "expression": "",
            "action": "",
            "composition": "",
            "mood": "",
            "draft_prompt": "",
            "resolution_hint": "",
            "deepseek_fallback": True,
        }

    result = _validate_chat_response(raw)
    result["reply"] = sanitize_public_agent_message(str(result.get("reply") or ""))
    steps = result.get("public_steps") or []
    result["public_steps"] = [sanitize_public_agent_message(str(s)) for s in steps]
    return result


def _build_user_prompt(
    *,
    user_message: str,
    memory_summary: str,
    recent_messages: list[dict[str, str]],
    workflow_summary: str,
    lora_summary: str,
    matched_characters: str,
    snippet_summary: str,
) -> str:
    parts = []

    if recent_messages:
        parts.append("Recent conversation:")
        for msg in recent_messages[-20:]:
            role_label = "击击" if msg["role"] == "user" else "小击击"
            parts.append(f"{role_label}: {msg['content']}")
        parts.append("")

    if memory_summary:
        parts.append(f"Memory（击击的需求记录）: {memory_summary}")
        parts.append("")

    parts.append("可用资源仅供你理解角色和术语；不要选择 workflow 或 LoRA：")
    parts.append(f"图片工作流:\n{workflow_summary}")
    parts.append(f"\nLoRA:\n{lora_summary}")
    if matched_characters and matched_characters != "- none":
        parts.append(f"\n角色 Tag:\n{matched_characters}")
    if snippet_summary and snippet_summary != "- none":
        parts.append(f"\n提示词片段库:\n{snippet_summary}")
    parts.append(f"\n可用画幅:\n{resolution_summaries()}")
    parts.append(f"\n\n击击的最新消息: {user_message}")
    parts.append("\n只返回 JSON。不要输出 can_generate、policy、拒绝理由、workflow_key 或 lora key。")

    return "\n".join(parts)


def _validate_chat_response(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeepSeekError("Agent returned invalid response")

    reply = str(raw.get("reply") or "")
    if not reply.strip() or _looks_like_refusal(reply):
        raw["reply"] = "我理解了你的需求喵～正在继续整理提示词。"

    memory_update = str(raw.get("memory_update") or "")[:2000]
    raw["memory_update"] = sanitize_public_agent_message(memory_update)

    public_steps = raw.get("public_steps")
    if not isinstance(public_steps, list):
        raw["public_steps"] = []
    else:
        raw["public_steps"] = [str(s)[:500] for s in public_steps[:10]]

    raw.pop("can_generate", None)
    raw.pop("refusal_reason", None)
    raw.pop("policy_reason", None)
    # Strip old character-related fields if DS still outputs them
    raw.pop("translated_prompt", None)
    raw.pop("character_candidates", None)
    raw.pop("characters", None)
    raw.pop("character_tags", None)
    raw.pop("character_name", None)
    raw.pop("franchise_tags", None)
    for key in (
        "scene",
        "style",
        "clothing",
        "expression",
        "action",
        "composition",
        "mood",
        "draft_prompt",
        "resolution_hint",
    ):
        value = str(raw.get(key) or "")
        if _looks_like_refusal(value):
            value = ""
        raw[key] = sanitize_public_agent_message(value)[:3000 if key == "translated_prompt" else 500]
    intent_suggestion = str(raw.get("intent_suggestion") or "chat").strip().lower()
    raw["intent_suggestion"] = intent_suggestion if intent_suggestion in {"chat", "generate", "regenerate", "edit"} else "chat"
    candidates = raw.get("character_candidates")
    if isinstance(candidates, list):
        raw["character_candidates"] = [sanitize_public_agent_message(str(item))[:120] for item in candidates[:8]]
    else:
        raw["character_candidates"] = []
    return raw


def _looks_like_refusal(text: str) -> bool:
    lowered = str(text or "").lower()
    refusal_markers = (
        "不符合我的生成原则",
        "不符合生成原则",
        "无法帮助",
        "不能帮助",
        "不能生成",
        "拒绝",
        "抱歉",
        "i can't",
        "i cannot",
        "cannot assist",
        "not able to help",
        "content policy",
        "policy",
        "safety",
    )
    return any(marker in lowered for marker in refusal_markers)


def chat_response_to_json(response: dict[str, Any]) -> str:
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))
