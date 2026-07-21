from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


# These are conversation controls, not visual prompt content.  They are removed
# only at the edges of a request so words such as "generation" inside an actual
# scene description are not accidentally lost.
_GENERATE_EDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:请|现在|直接|马上|立刻|就|可以|那就|然后|处理好(?:以后|之后|就)?|整理好(?:以后|之后|就)?)?"
        r"(?:帮我|给我|为我)?(?:开始)?(?:生成|生图|出图|画图|画一张)(?:吧|了|即可|就行|就好|喵)?[，,。.!！?？\s]*",
        re.I,
    ),
    re.compile(
        r"[，,。.!！?？\s]*(?:处理好(?:以后|之后|就)?|整理好(?:以后|之后|就)?|准备好(?:以后|之后|就)?)?"
        r"(?:请|现在|直接|马上|立刻|就|可以|那就|然后)?(?:帮我|给我|为我)?"
        r"(?:开始)?(?:生成|生图|出图|画图)(?:吧|了|即可|就行|就好|喵)?$",
        re.I,
    ),
)

_GENERATE_EXACT = {
    "生成",
    "生图",
    "出图",
    "画图",
    "开始生成",
    "现在生成",
    "直接生成",
    "帮我生成",
    "给我生成",
    "生成吧",
    "出图吧",
    "可以生成了",
    "就这样生成",
    "按这个生成",
    "按刚才的生成",
    "处理好就生成",
    "整理好就生成",
}

_GENERATE_MARKERS = (
    "生成", "生图", "出图", "画一张", "画图", "开始画", "render", "generate",
)

_PROMPT_EXPOSURE_MARKERS = (
    "查看最终prompt", "查看prompt", "看看prompt", "显示prompt", "给我prompt",
    "英文prompt", "最终提示词", "完整提示词", "内部提示词", "看提示词",
    "show prompt", "display prompt", "reveal prompt", "full prompt",
)

_QUERY_MARKERS = (
    "当前人物", "现在的人物", "选了谁", "现在是谁", "有哪些人物", "有哪些角色",
    "who is selected", "current character", "selected character",
)

_ADD_MARKERS = ("加入", "添加", "加上", "追加", "一起", "也要", "add", "include", "also")
_REPLACE_MARKERS = ("换成", "改成", "替换", "改为", "换为", "switch to", "replace", "change to")
_REMOVE_MARKERS = ("删除", "去掉", "移除", "不要这个人物", "remove", "delete", "drop")

_SECRET_OR_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\b(?:api[_ -]?key|token|cookie|session|password|secret)\b", re.I),
    re.compile(r"\b[A-Za-z]:\\[^\r\n]*"),
    re.compile(r"(?:/home/|/mnt/|/Users/)[^\s`\"']+"),
    re.compile(r"\b[^\s]+\.(?:py|db|sqlite|json|xlsx|safetensors|ps1)\b", re.I),
)


@dataclass(frozen=True)
class TurnRequest:
    raw_text: str
    visual_text: str
    generation_requested: bool
    meta_only: bool
    prompt_exposure_requested: bool
    turn_key: str


def _compact(text: str) -> str:
    return re.sub(r"[\s，,。.!！?？~～]+", "", str(text or "").strip().lower())


def generation_requested(text: str, resolved_intent: str = "") -> bool:
    raw = str(text or "").strip()
    compact = _compact(raw)
    if compact in {_compact(item) for item in _GENERATE_EXACT}:
        return True
    if str(resolved_intent or "").strip().lower() in {"generate", "regenerate", "edit"}:
        return True
    lowered = raw.lower()
    # Require an imperative context for a marker in a longer sentence.  This
    # avoids interpreting statements such as "这是生成结果" as a new task.
    imperative = any(
        marker in lowered
        for marker in (
            "帮我", "给我", "请", "直接", "开始", "现在", "就", "处理好", "整理好",
            "can you", "please", "generate", "render",
        )
    )
    return imperative and any(marker in lowered for marker in _GENERATE_MARKERS)


def strip_generation_controls(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    previous = None
    while value != previous:
        previous = value
        for pattern in _GENERATE_EDGE_PATTERNS:
            value = pattern.sub("", value).strip(" ，,。.!！?？~～")
    return value.strip()


def is_prompt_exposure_request(text: str) -> bool:
    compact = _compact(text)
    if any(_compact(marker) in compact for marker in _PROMPT_EXPOSURE_MARKERS):
        return True
    # Natural requests often put modifiers between the action and the noun,
    # for example "我看看你整理的提示词".  Exact marker matching misses
    # those phrases and lets the request reach the model, which may then emit
    # only an empty preamble while still obeying the no-prompt-exposure rule.
    asks_to_show = any(
        marker in compact
        for marker in (
            "看", "查看", "给我", "发我", "输出", "贴出",
            "show", "reveal", "print",
        )
    )
    mentions_prompt = "prompt" in compact or "提示词" in compact
    shows_prompt = bool(
        re.search(r"(?:展示|显示|display).{0,12}(?:提示词|prompt)", compact, re.I)
        or (
            ("展示出来" in compact or "显示出来" in compact)
            and mentions_prompt
        )
    )
    return mentions_prompt and (asks_to_show or shows_prompt)


def prepare_turn(
    text: str,
    *,
    resolved_intent: str = "",
    client_request_id: str | None = None,
    message_id: int | None = None,
) -> TurnRequest:
    raw = str(text or "").strip()
    wants_generation = generation_requested(raw, resolved_intent)
    visual = strip_generation_controls(raw) if wants_generation else raw
    meta_only = wants_generation and not visual
    seed = f"{client_request_id or ''}:{message_id or 0}:{raw}"
    turn_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return TurnRequest(
        raw_text=raw,
        visual_text=visual,
        generation_requested=wants_generation,
        meta_only=meta_only,
        prompt_exposure_requested=is_prompt_exposure_request(raw),
        turn_key=turn_key,
    )


def resolve_character_operation_v2(
    text: str,
    *,
    has_current: bool,
    has_new_characters: bool,
) -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if any(marker in lowered for marker in _QUERY_MARKERS):
        return "query_characters"
    if has_new_characters and any(marker in lowered for marker in _REMOVE_MARKERS):
        return "remove_characters"
    if has_new_characters and any(marker in lowered for marker in _ADD_MARKERS):
        return "add_characters"
    if has_new_characters and any(marker in lowered for marker in _REPLACE_MARKERS):
        return "replace_characters"
    if has_new_characters:
        return "replace_characters" if has_current else "add_characters"
    if has_current:
        return "generation_supplement"
    return "ordinary_chat"


def safe_previous_state(draft: dict[str, Any] | None) -> dict[str, Any]:
    """Return only non-sensitive, non-prompt state for the model."""
    if not draft:
        return {}
    result: dict[str, Any] = {}
    try:
        structured = json.loads(str(draft.get("structured_draft_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        structured = {}
    if isinstance(structured, dict):
        for key in (
            "scene", "style", "clothing", "expression", "action", "composition",
            "lighting", "mood", "resolution_hint",
        ):
            value = str(structured.get(key) or "").strip()
            if value:
                result[key] = value[:500]
    request_text = str(draft.get("request_text") or "").strip()
    if request_text:
        result["previous_user_request"] = request_text[:1200]
    return result


def safe_recent_messages(messages: Iterable[dict[str, Any]], limit: int = 8) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in list(messages)[-max(1, int(limit)):]:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("safe_content") or message.get("content") or "").strip()
        if not content:
            continue
        # Status/progress messages should not become visual requirements.
        if role == "assistant" and any(
            marker in content
            for marker in (
                "正在处理", "正在整理", "正在识别", "正在匹配", "任务已加入队列",
                "提示词已整理完成", "Smart Agent 暂时", "消息已发送",
            )
        ):
            continue
        result.append({"role": role, "content": sanitize_public_text(content)[:1000]})
    return result[-limit:]


def sanitize_public_text(text: str, fallback: str = "") -> str:
    value = str(text or "").strip()
    for pattern in _SECRET_OR_INTERNAL_PATTERNS:
        value = pattern.sub("[已隐藏]", value)
    value = re.sub(r"```(?:json)?", "", value, flags=re.I).replace("```", "")
    value = value.strip()
    return value[:1500] if value else fallback


def safe_prompt_hidden_reply() -> str:
    return "内部生图 Prompt 不直接展示。你可以继续告诉我要修改的场景、服装、动作、表情或构图，也可以直接说开始生成。"


def character_display(characters: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in characters:
        key = str(
            item.get("character_key")
            or item.get("identity_key")
            or item.get("key")
            or item.get("name_en")
            or item.get("name_zh")
            or ""
        ).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({
            "character_id": key,
            "name": str(item.get("name_zh") or item.get("name_en") or key)[:120],
            "series": str(
                item.get("franchise_zh")
                or item.get("category_zh")
                or item.get("franchise_en")
                or item.get("category_en")
                or ""
            )[:120],
        })
    return result


def dedupe_tags(value: str, limit: int = 80) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").replace("，", ",").split(","):
        tag = " ".join(raw.strip().split())
        key = tag.lower().replace("_", " ")
        if not tag or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= limit:
            break
    return ", ".join(result)
