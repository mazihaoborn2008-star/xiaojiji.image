from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.config import Settings
from app.content_policy import should_apply_adult_content_filter
from app.db import validate_resolution

from .character_search import find_characters, extract_possible_character_names, translate_character_name, build_agent_fallback_character
from .character_preferences import (
    CharacterPromptValidationError,
    character_key,
    enforce_character_preferences,
    public_character_matches,
    validate_character_prompt,
    validate_multi_character_prompt,
)
from .deepseek_client import DeepSeekError, complete_json
from .lora_registry import lora_summaries, sanitize_loras
from .prompt_library import search_prompt_snippets, snippets_for_prompt
from .workflow_registry import get_workflow, workflow_summaries
from .dynamic_workflows import SMART_AGENT_DEFAULT_WORKFLOW_KEY


class SmartAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str = "smart_agent_error"):
        super().__init__(message)
        self.code = code


class SmartAgentClarification(SmartAgentError):
    def __init__(self, question: str):
        super().__init__(question, code="smart_agent_needs_clarification")


# ── Character decision classification ───────────────────────────────

# Validation limits for character IDs.
MAX_CHARACTERS_PER_TASK = 8
MAX_CHARACTER_ID_LENGTH = 80
MAX_CHARACTER_IDS_JSON_LENGTH = 1024

# All known task-level prompt_source values that carry a character decision.
_RESOLVED_SOURCES = {"agent_character_resolved"}
_DISABLED_SOURCES = {"agent_character_no_library", "agent_no_character"}
# Values that are valid but carry no character decision (legacy/direct).
_LEGACY_SOURCES = {"", "smart_agent", "smart_agent+character_registry", "smart_agent_v2"}
_KNOWN_SOURCES = _RESOLVED_SOURCES | _DISABLED_SOURCES | _LEGACY_SOURCES


def _classify_task_character_decision(prompt_source: str) -> str:
    """Classify a task's prompt_source into an internal character decision state.

    Returns:
        "resolved"  — user confirmed specific characters
        "disabled"  — user chose to skip library, or no character found
        "legacy"    — no character decision data (historical tasks, direct prompts)
        "invalid"   — unknown or contradictory state; must not fall back to search
    """
    if prompt_source in _RESOLVED_SOURCES:
        return "resolved"
    if prompt_source in _DISABLED_SOURCES:
        return "disabled"
    if prompt_source in _LEGACY_SOURCES:
        return "legacy"
    # Unknown non-empty value → invalid (do NOT treat as legacy)
    return "invalid"


def serialize_character_ids(character_ids: list[str]) -> str:
    """Serialize a list of character IDs to a compact JSON array string.

    Deduplicates while preserving order.  Returns "[]" for empty input.
    Validates: max 8 IDs, each max 80 chars, total max 1024 chars.
    """
    seen: set[str] = set()
    result: list[str] = []
    for cid in character_ids:
        cid = str(cid or "").strip()
        if not cid:
            continue
        if len(cid) > MAX_CHARACTER_ID_LENGTH:
            raise SmartAgentError(
                "人物 ID 过长，请重新选择。",
                code="invalid_character_resolution",
            )
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    if len(result) > MAX_CHARACTERS_PER_TASK:
        raise SmartAgentError(
            f"人物数量不能超过 {MAX_CHARACTERS_PER_TASK} 个。",
            code="invalid_character_resolution",
        )
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > MAX_CHARACTER_IDS_JSON_LENGTH:
        raise SmartAgentError(
            "人物数据过长，请减少人物数量或缩短 ID。",
            code="invalid_character_resolution",
        )
    return serialized


def parse_character_ids(raw: str) -> list[str]:
    """Parse a character_key field into a list of character IDs.

    Supports both formats:
    - New JSON array: ["id1","id2"]
    - Legacy comma-separated: "id1,id2"

    Rules:
    - String starting with [ must parse as strict JSON.
    - JSON parse failure does NOT fall back to comma split.
    - Legacy non-JSON strings are split by comma.
    - Returns [] for empty/invalid input.
    """
    raw = str(raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # JSON parse failure → return empty (do NOT fall back to comma split)
            return []
        if not isinstance(parsed, list):
            return []
        result: list[str] = []
        for item in parsed:
            if not isinstance(item, str):
                return []  # non-string element → invalid
            s = item.strip()
            if not s:
                return []  # empty string element → invalid
            result.append(s)
        return result
    # Legacy comma-separated format
    return [s.strip() for s in raw.split(",") if s.strip()]


def validate_character_ids(char_ids: list[str], *, source: str = "") -> list[str]:
    """Validate a list of parsed character IDs.

    Checks:
    - Non-empty (for resolved state)
    - Max 8 characters
    - Each ID max 80 chars
    - No NO_LIBRARY_CHARACTER_ID sentinel
    - No duplicates (preserves order)

    Returns deduplicated list or raises SmartAgentError.
    """
    if not char_ids:
        raise SmartAgentError(
            "人物选择结果无效，请重新选择。",
            code="invalid_character_resolution",
        )
    seen: set[str] = set()
    result: list[str] = []
    for cid in char_ids:
        cid = str(cid or "").strip()
        if not cid:
            raise SmartAgentError(
                "人物选择结果无效，请重新选择。",
                code="invalid_character_resolution",
            )
        if cid == "__no_library_character__":
            raise SmartAgentError(
                "人物选择结果无效，请重新选择。",
                code="invalid_character_resolution",
            )
        if len(cid) > MAX_CHARACTER_ID_LENGTH:
            raise SmartAgentError(
                "人物 ID 过长，请重新选择。",
                code="invalid_character_resolution",
            )
        if cid not in seen:
            seen.add(cid)
            result.append(cid)
    if len(result) > MAX_CHARACTERS_PER_TASK:
        raise SmartAgentError(
            f"人物数量不能超过 {MAX_CHARACTERS_PER_TASK} 个。",
            code="invalid_character_resolution",
        )
    return result


ALWAYS_FORBIDDEN_REQUEST_PATTERNS = [
    r"\b(video|animation|animated|mp4|gif|movie clip)\b",
    r"(未成年.*裸|儿童.*色情|儿童.*裸)",
    r"\b(child sexual|minor nude)\b",
]

ADULT_FILTER_REQUEST_PATTERNS = [
    r"(裸|色情|性爱|成人向|成人|成年|私房|写真)",
    r"\b(nude|porn|sex|sexual|adult|mature)\b",
]

FORBIDDEN_PLAN_PATTERNS = [
    r"[a-zA-Z]:[\\/]",
    r"https?://",
    r"\b(cmd\.exe|powershell|python\s+-|bash|curl|wget)\b",
    r"\.\.[\\/]",
]


async def build_smart_agent_plan(
    settings: Settings,
    request_text: str,
    *,
    is_admin: bool = False,
    task_prompt_source: str = "",
    task_character_key: str = "",
) -> dict[str, Any]:
    request_text = (request_text or "").strip()
    if not request_text:
        raise SmartAgentError("请输入想生成的画面。", code="smart_agent_empty")
    if len(request_text) > 1200:
        raise SmartAgentError("需求描述最多 1200 字。", code="smart_agent_too_long")
    _validate_request_policy(settings, request_text)

    # ── Respect pre-resolved character decision from the task ──
    # When the task was created through the web form, the user already made
    # a character decision (exact match, "都不是", or no character found).
    # The Worker must NOT re-run character matching and override that.
    characters: list[dict[str, Any]] = []
    translated_character_name = ""
    original_character_name = ""
    character_tag_source = ""

    decision = _classify_task_character_decision(task_prompt_source)

    if decision == "resolved":
        # User selected specific characters → use them directly
        from .disambiguation_engine import characters_from_public_ids
        char_ids = validate_character_ids(parse_character_ids(task_character_key))
        characters = characters_from_public_ids(char_ids)
        loaded_ids = {
            str(item.get("key") or item.get("identity_key") or "").strip()
            for item in characters
            if str(item.get("key") or item.get("identity_key") or "").strip()
        }
        missing_ids = [cid for cid in char_ids if cid not in loaded_ids]
        if missing_ids:
            raise SmartAgentError(
                "人物选择结果无效，请重新选择。",
                code="invalid_character_resolution",
            )
        for item in characters:
            item["character_tag_source"] = "character_registry"
            item["match_stage"] = "confirmed_resolution"
        character_tag_source = "character_registry"
    elif decision == "disabled":
        # User chose "都不是" (skip library) or no character found → no library matching
        # Disabled state must NOT have character IDs
        disabled_ids = parse_character_ids(task_character_key)
        if disabled_ids:
            raise SmartAgentError(
                "人物选择结果无效，请重新选择。",
                code="invalid_character_resolution",
            )
        character_tag_source = "none"
    elif decision == "invalid":
        # Unknown or contradictory state → safe failure, no fallback
        raise SmartAgentError(
            "人物选择结果无效，请重新选择。",
            code="invalid_character_resolution",
        )
    else:
        # legacy: no character decision data → match from scratch
        characters = _dedupe_character_matches(find_characters(request_text))
        if not characters:
            candidate_cn = extract_possible_character_names(request_text)
            if candidate_cn:
                original_character_name = candidate_cn
                translated = await translate_character_name(candidate_cn)
                if translated and translated != candidate_cn:
                    translated_character_name = translated
                    characters = _dedupe_character_matches(find_characters(translated))
                    if characters:
                        character_tag_source = "character_registry"
                        for item in characters:
                            item["character_tag_source"] = "character_registry"
                            item["match_stage"] = "translated"
        if not characters and translated_character_name:
            fallback = build_agent_fallback_character(translated_character_name, original_character_name)
            if fallback:
                characters = [fallback]
                character_tag_source = "agent_fallback"
    if characters:
        character_tag_source = character_tag_source or str(characters[0].get("character_tag_source") or "character_tags")
    snippets = search_prompt_snippets(request_text)
    system_prompt = _system_prompt()
    user_prompt = _user_prompt(
        request_text=request_text,
        workflows=workflow_summaries(is_admin=is_admin),
        loras=lora_summaries(),
        characters=characters,
        snippets=snippets,
    )
    try:
        raw_plan = await complete_json(settings, system_prompt=system_prompt, user_prompt=user_prompt)
    except DeepSeekError as exc:
        raise SmartAgentError("Smart Agent 暂时无法规划，请稍后重试。", code=str(exc)) from exc

    plan = _validate_plan(raw_plan, is_admin=is_admin)
    if plan.get("needs_clarification"):
        question = str(plan.get("clarification_question") or "请补充想要生成的画面。")[:300]
        raise SmartAgentClarification(question)

    workflow_key = str(plan["workflow_key"])
    positive = str(plan["positive_prompt"]).strip()
    negative = str(plan.get("negative_prompt") or "").strip()
    width, height = validate_resolution(int(plan.get("width") or 1024), int(plan.get("height") or 1536))
    loras = sanitize_loras(plan.get("loras"), workflow_key)
    enforced = enforce_character_preferences(
        characters=characters,
        workflow_key=workflow_key,
        positive_prompt=positive,
        loras=loras,
        is_admin=is_admin,
        request_text=request_text,
    )
    workflow_key = str(enforced["workflow_key"])
    positive = str(enforced["positive_prompt"])
    loras = list(enforced["loras"])
    if character_tag_source != "agent_fallback" and character_key(characters[0] if characters else None):
        try:
            if len(characters) > 1:
                validate_multi_character_prompt(
                    prompt=positive,
                    characters=characters,
                    workflow_key=workflow_key,
                    loras=loras,
                    user_text=request_text,
                )
            else:
                validate_character_prompt(
                    prompt=positive,
                    character=characters[0] if characters else None,
                    workflow_key=workflow_key,
                    loras=loras,
                )
        except CharacterPromptValidationError as exc:
            user_msg = "人物提示词整理失败，请重新尝试；如果该人物不在人物库中，系统将使用翻译后的名称继续生成。"
            raise SmartAgentError(user_msg, code="character_prompt_validation_failed") from exc
    _validate_plan_text(positive)
    _validate_plan_text(negative)
    source_ids = [str(item.get("id")) for item in snippets[:10] if item.get("id")]
    prompt_source = "character_tags+prompt_library+deepseek" if source_ids or characters else "deepseek"
    if enforced.get("forced"):
        prompt_source += "+character_registry"
    validated = {
        "workflow_key": workflow_key,
        "width": width,
        "height": height,
        "positive_prompt": positive[:3000],
        "negative_prompt": negative[:1600],
        "loras": loras,
        "prompt_source": prompt_source,
        "fallback_level": enforced.get("fallback_level") or ("character_tags" if characters else "none"),
        "character_key": character_key(characters[0]) if characters else "",
        "locked_character_tags": enforced.get("locked_character_tags") or [],
        "foreign_character_tags_removed_count": int(enforced.get("foreign_character_tags_removed_count") or 0),
        "character_workflow_key": enforced.get("character_workflow_key") or "",
        "allow_external_lora": bool(enforced.get("allow_external_lora")),
        "matched_characters": public_character_matches(characters),
        "library_snippet_ids": source_ids,
        "request_hash": hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:12],
        "character_tag_source": character_tag_source,
        "translated_character_name": translated_character_name,
        "original_character_name": original_character_name,
    }
    return validated


def _dedupe_character_matches(characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in characters:
        key = character_key(item)
        # agent_fallback 角色可能 key 为空，使用 name_en 作为去重键
        dedup_key = key or str(item.get("character_tag_source") or "") + ":" + str(item.get("name_en") or "")
        if not key and not dedup_key:
            continue
        if dedup_key in seen:
            continue
        clean = dict(item)
        clean["key"] = key
        result.append(clean)
        seen.add(dedup_key)
    return result


def _system_prompt() -> str:
    return (
        "You are Smart Agent for an anime SDXL/Anima image generation website. "
        "Return only a valid JSON object. You cannot execute commands, access files, browse URLs, "
        "control the computer, reveal paths, or choose anything outside server-provided whitelists. "
        "Image only: reject video/animation requests by setting needs_clarification=true. "
        "Use concise booru-style English prompt tags. Never include Chinese in prompts unless it is a proper title tag. "
        "Character tool priority: character-specific workflow first, then generic workflow plus character LoRA, "
        "then generic workflow plus character tags. Do not invent workflow or LoRA keys. "
        "Schema: {needs_clarification:boolean, clarification_question:string, workflow_key:string, width:int, height:int, "
        "positive_prompt:string, negative_prompt:string, loras:[{key:string, weight:number}], reasoning:string}. "
        "reasoning must be short and non-sensitive."
    )


def _user_prompt(*, request_text: str, workflows: str, loras: str, characters: list[dict[str, str]], snippets: list[dict[str, Any]]) -> str:
    character_text = "\n".join(
        f"- {item['name_zh']} / {item['name_en']} ({item['category_en']}): {item['tags']}"
        for item in characters
    ) or "- none"
    return (
        "User request:\n"
        f"{request_text}\n\n"
        "Allowed image workflows:\n"
        f"{workflows}\n\n"
        "Allowed LoRA registry:\n"
        f"{loras}\n\n"
        "Matched character tags, mandatory if relevant:\n"
        f"{character_text}\n\n"
        "Prompt library snippets:\n"
        f"{snippets_for_prompt(snippets)}\n\n"
        "Choose exactly one workflow_key from the allowed workflows. If a matched character has a dedicated workflow in "
        "the allowed list, prefer that workflow over the default generic workflow. If no dedicated character workflow is available, "
        "use a compatible generic workflow and character LoRA when whitelisted; otherwise rely on character tags. "
        f"The default generic workflow key is {SMART_AGENT_DEFAULT_WORKFLOW_KEY}."
        "Return JSON only."
    )


def _validate_request_policy(settings: Settings, text: str) -> None:
    lowered = text.lower()
    for pattern in ALWAYS_FORBIDDEN_REQUEST_PATTERNS:
        if re.search(pattern, lowered, re.I):
            raise SmartAgentError("Smart Agent 目前只支持安全的图片生成请求。", code="smart_agent_policy_blocked")
    if not should_apply_adult_content_filter(settings):
        return
    for pattern in ADULT_FILTER_REQUEST_PATTERNS:
        if re.search(pattern, lowered, re.I):
            raise SmartAgentError("Smart Agent 目前只支持安全的图片生成请求。", code="smart_agent_policy_blocked")


def _validate_plan(raw: dict[str, Any], *, is_admin: bool) -> dict[str, Any]:
    if bool(raw.get("needs_clarification")):
        return raw
    workflow_key = str(raw.get("workflow_key") or "").strip()
    if not get_workflow(workflow_key, is_admin=is_admin):
        raise SmartAgentError("Smart Agent 选择了不可用的工作流，已退款。", code="smart_agent_invalid_workflow")
    prompt = str(raw.get("positive_prompt") or "").strip()
    if not prompt:
        raise SmartAgentError("Smart Agent 没有生成可用 Prompt，已退款。", code="smart_agent_empty_prompt")
    return raw


def _validate_plan_text(text: str) -> None:
    lowered = text.lower()
    for pattern in FORBIDDEN_PLAN_PATTERNS:
        if re.search(pattern, lowered, re.I):
            raise SmartAgentError("Smart Agent 返回内容包含不允许的信息，已退款。", code="smart_agent_unsafe_plan")


def _merge_prompt_tags(tag_groups: list[str], prompt: str) -> str:
    tags: list[str] = []
    seen = set()
    for group in tag_groups:
        for raw in str(group or "").split(","):
            tag = raw.strip()
            if tag and tag.lower() not in seen:
                tags.append(tag)
                seen.add(tag.lower())
    prompt_parts = [part.strip() for part in prompt.split(",") if part.strip()]
    for part in prompt_parts:
        if part.lower() not in seen:
            tags.append(part)
            seen.add(part.lower())
    return ", ".join(tags)


def plan_to_json(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
