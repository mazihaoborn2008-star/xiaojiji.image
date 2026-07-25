import asyncio
import re
import json
import time

import httpx

from .config import Settings
from .content_policy import should_apply_adult_content_filter
from .smart_agent.character_search import find_character_after_translation
from .smart_agent.disambiguation_engine import characters_from_public_ids
from .smart_agent.character_preferences import (
    enforce_character_preferences,
    sanitize_inferred_appearance_tags,
    validate_character_prompt,
    CharacterPromptValidationError,
)

MAX_REFINED_PROMPT_CHARS = 2000
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
PREFIX_RE = re.compile(
    r"^\s*(?:final\s+prompt|prompt|output|result|answer|converted\s+prompt|english\s+prompt|tags?)\s*[:：-]\s*",
    re.IGNORECASE,
)


def parse_generation_task_character_resolution(
    character_key_field: str | list[str] | tuple[str, ...] | None,
    *,
    prompt_source: str | None = None,
) -> tuple[list[str], bool]:
    """Parse stored generation_tasks.character_key for the worker-side Agent.

    New web submissions store selected character ids as a JSON array string
    (for example ["vivlos"]).  Older worker code treated the field as a
    comma-separated string, which made the literal JSON text look like one
    invalid id.  Keep the legacy comma format as a fallback for old rows.
    """
    source = str(prompt_source or "").strip()
    raw = character_key_field
    if source == "agent_character_no_library":
        return [], True
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item or "").strip()], False
    text = str(raw or "").strip()
    if not text or text == "[]":
        return [], False
    if text == "__no_library_character__":
        return [], True
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            ids = [str(item).strip() for item in parsed if str(item or "").strip()]
            return ids, False
    ids = [
        part.strip()
        for part in text.split(",")
        if part.strip() and part.strip() != "__no_library_character__"
    ]
    return ids, False
THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
AGENT_SEMAPHORE: asyncio.Semaphore | None = None
AGENT_SEMAPHORE_LIMIT = 0
RETRYABLE_AGENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

BASE_SYSTEM_PROMPT = """
You are a prompt converter for anime image generation models.

Task:
Chinese natural language or Chinese keywords -> understand the visual meaning -> extract visual elements -> convert to English booru-style tags -> output a ComfyUI-ready prompt.

Output rules:
1. Return exactly one line of English prompt.
2. Use comma-separated short tags: tag1, tag2, tag3.
3. Do not output explanations, titles, markdown, JSON, Chinese, paragraphs, or negative prompts.
4. Prefer concise common anime / SDXL / Anima / booru-style tags.
5. Do not invent character names or important scene details.
6. Do not repeat tags.
7. Do not add large sets of generic quality tags.
8. Unless the user explicitly describes hair color, eye color, hairstyle, hair length, skin, or body shape, do not infer or add appearance tags from character knowledge. If a character is not in the registry, translate only the character name and do not describe their appearance.

Language handling:
- Chinese input: understand the meaning, extract visual elements, and convert to English tags. Do not keep Chinese and do not translate word-by-word mechanically.
- English tag input: preserve the user's English tags as much as possible; only deduplicate, lightly normalize, and fix obvious spelling.
- Mixed Chinese/English input: preserve English tags, convert Chinese parts to English tags, merge and deduplicate. Final output must contain no Chinese.

Recommended tag order:
character count -> existing character tags -> clothing -> action/pose -> expression -> camera/composition -> scene -> lighting -> atmosphere -> a few style tags.

Useful conversions:
眺望远方 -> looking into the distance
站在草地上 -> standing on grass
回头看镜头 -> looking back at viewer
阳光从窗户照进来 -> sunlight through window
坐在公园长椅上 -> sitting on a park bench
高机位俯视 -> from above, high angle
正面视角 -> front view
全身构图 -> full body
害羞地微笑 -> shy smile, blushing
风吹起头发和裙摆 -> wind, hair blowing, skirt fluttering

If information is missing, use the most conservative conversion and add only minimal relationship tags such as outdoor when needed.
""".strip()

ADULT_FILTER_SYSTEM_RULE = (
    "Do not add explicit, nude, sexual, or revealing elements unless the user clearly asks."
)


def system_prompt_for_settings(settings: Settings) -> str:
    if should_apply_adult_content_filter(settings):
        return f"{BASE_SYSTEM_PROMPT}\n\nAdditional content rule:\n{ADULT_FILTER_SYSTEM_RULE}"
    return BASE_SYSTEM_PROMPT


def _strip_code_fences(content: str) -> str:
    text = content.strip()
    text = THINK_RE.sub("", text).strip()
    if "</think>" in text.lower():
        text = re.sub(r"^.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    fenced = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    text = re.sub(r"^\s*```(?:[A-Za-z0-9_-]+)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _pick_prompt_line(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ""
    cleaned_lines = []
    for line in lines:
        line = PREFIX_RE.sub("", line).strip()
        if line:
            cleaned_lines.append(line)
    if not cleaned_lines:
        return ""
    for line in cleaned_lines:
        if "," in line and not line.endswith(":"):
            return line
    return cleaned_lines[-1]


def clean_agent_prompt(content: str) -> str:
    text = _strip_code_fences(content)
    text = _pick_prompt_line(text)
    text = PREFIX_RE.sub("", text).strip()
    text = text.strip(" \t\r\n\"'`“”‘’")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = text.strip(" ,")
    if not text:
        raise RuntimeError("Agent 转换失败：返回为空，已保留原 Prompt")
    if CHINESE_RE.search(text):
        raise RuntimeError("Agent 转换失败：返回内容包含中文，已保留原 Prompt")
    text = _normalize_common_tags(text)
    return text[:MAX_REFINED_PROMPT_CHARS]


def _normalize_common_tags(text: str) -> str:
    tags = [tag.strip() for tag in text.split(",") if tag.strip()]
    normalized = []
    for tag in tags:
        low = tag.lower()
        if low in {"looking into distance", "looking at horizon", "gaze forward"}:
            tag = "looking into the distance"
        elif low == "outdoors":
            tag = "outdoor"
        elif low == "sitting on park bench":
            tag = "sitting on a park bench"
        elif low == "park bench" and any(t.lower() == "sitting" for t in tags):
            tag = "sitting on a park bench"
        normalized.append(tag)
    lows = {tag.lower() for tag in normalized}
    if "standing" in lows and ("grass" in lows or "grass field" in lows) and "standing on grass" not in lows:
        insert_at = next((i for i, tag in enumerate(normalized) if tag.lower() == "standing"), len(normalized))
        normalized.insert(insert_at, "standing on grass")
    if "sitting" in lows and "park bench" in lows and "sitting on a park bench" not in lows:
        insert_at = next((i for i, tag in enumerate(normalized) if tag.lower() == "sitting"), len(normalized))
        normalized.insert(insert_at, "sitting on a park bench")
    if "sitting" in lows and "bench" in lows and "sitting on a park bench" not in lows:
        insert_at = next((i for i, tag in enumerate(normalized) if tag.lower() == "sitting"), len(normalized))
        normalized.insert(insert_at, "sitting on a park bench")
    if "sitting on a park bench" in {tag.lower() for tag in normalized} and "park" not in {tag.lower() for tag in normalized}:
        normalized.append("park")
    result = []
    seen = set()
    for tag in normalized:
        low = tag.lower()
        if low in seen:
            continue
        seen.add(low)
        result.append(tag)
    return ", ".join(result)


async def refine_prompt(
    settings: Settings,
    text: str,
    *,
    resolved_character_ids: list[str] | None = None,
    disable_character_library: bool = False,
) -> str:
    if not settings.agent_enabled:
        raise RuntimeError("Agent 未启用")
    semaphore = _get_agent_semaphore(settings)
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=_agent_acquire_timeout(settings))
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Agent 正忙，请稍后再试") from exc
    try:
        if settings.agent_provider.lower() == "ollama":
            prompt = await _refine_prompt_ollama(settings, text)
        else:
            prompt = await _refine_prompt_openai_compatible(settings, text)
        return _apply_character_registry_to_refined_prompt(
            text,
            prompt,
            resolved_character_ids=resolved_character_ids,
            disable_character_library=disable_character_library,
        )
    finally:
        semaphore.release()


def _agent_concurrency_limit(settings: Settings) -> int:
    try:
        return max(1, int(settings.agent_max_concurrency or 1))
    except (TypeError, ValueError):
        return 1


def _agent_acquire_timeout(settings: Settings) -> float:
    try:
        configured_timeout = float(settings.agent_timeout_seconds or 30)
    except (TypeError, ValueError):
        configured_timeout = 30.0
    return min(10.0, max(2.0, configured_timeout * 0.2))


def _get_agent_semaphore(settings: Settings) -> asyncio.Semaphore:
    global AGENT_SEMAPHORE, AGENT_SEMAPHORE_LIMIT
    limit = _agent_concurrency_limit(settings)
    if AGENT_SEMAPHORE is None or AGENT_SEMAPHORE_LIMIT != limit:
        AGENT_SEMAPHORE = asyncio.Semaphore(limit)
        AGENT_SEMAPHORE_LIMIT = limit
    return AGENT_SEMAPHORE


async def _post_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = 2,
    **kwargs,
) -> httpx.Response:
    last_exc: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            response = await client.post(url, **kwargs)
            if (
                response.status_code in RETRYABLE_AGENT_HTTP_STATUSES
                and attempt + 1 < max(1, attempts)
            ):
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return response
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
            if attempt + 1 >= max(1, attempts):
                raise
            await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Agent 暂时不可用，请稍后再试")


def _strip_mistranslated_character_names(refined_prompt: str, character: dict) -> str:
    """剥离 LLM 翻译出的错误人名 tag。

    当人物通过原文中文 alias 匹配成功时，LLM 可能把中文名音译成拼音
    （如「强击」→「qianji」），这些 tag 必须在 canonical Prompt 组装前移除。
    """
    tags = [tag.strip() for tag in refined_prompt.split(",")]
    if not tags:
        return refined_prompt

    # 收集人物所有已知别名（大小写不敏感）
    known: set[str] = set()
    known.add(str(character.get("name_en") or "").strip().lower())
    known.add(str(character.get("key") or "").strip().lower().replace("_", " "))
    for alias in (character.get("aliases") or "").replace("，", ",").split(","):
        alias = alias.strip().lower()
        if alias:
            known.add(alias)

    # 这些 tag 作为首位置不被剥离
    safe_first: set[str] = {
        "1girl", "1boy", "solo", "couple", "2girls", "2boys",
        "multiple girls", "multiple boys", "group",
    }

    stripped: list[str] = []
    stripped_count = 0
    for i, tag in enumerate(tags):
        tag_lower = tag.lower()
        # 仅检查前 3 个 tag 位置，最多剥离 2 个
        if stripped_count < 2 and i < 3:
            is_single_ascii_word = (" " not in tag) and tag.isascii() and tag.islower()
            if is_single_ascii_word and tag_lower not in known and tag_lower not in safe_first:
                stripped_count += 1
                continue
        stripped.append(tag)

    return ", ".join(stripped)


def _dedupe_characters_by_identity(characters: list[dict]) -> list[dict]:
    """按人物身份去重。同一个人物可能在多个数据源有记录（如 character-tags.json 和 character_registry.json）。"""
    if len(characters) <= 1:
        return list(characters)
    result: list[dict] = []
    seen_keys: set[str] = set()
    seen_names: set[str] = set()
    for c in characters:
        c_key = str(c.get("key") or "").strip()
        c_name_zh = str(c.get("name_zh") or "").strip()
        c_name_en = str(c.get("name_en") or "").strip().lower()
        # 优先用 key 去重，其次用中文名，最后用英文名
        if c_key and c_key in seen_keys:
            continue
        if c_name_zh and c_name_zh in seen_names:
            continue
        if c_name_en and c_name_en in seen_names:
            continue
        if c_key:
            seen_keys.add(c_key)
        if c_name_zh:
            seen_names.add(c_name_zh)
        if c_name_en:
            seen_names.add(c_name_en)
        result.append(c)
    return result


def _apply_character_registry_to_refined_prompt(
    original_text: str,
    refined_prompt: str,
    *,
    resolved_character_ids: list[str] | None = None,
    disable_character_library: bool = False,
) -> str:
    if disable_character_library:
        cleaned, _, _ = sanitize_inferred_appearance_tags(
            refined_prompt,
            user_text=original_text,
            character=None,
        )
        return cleaned[:MAX_REFINED_PROMPT_CHARS]

    explicit_resolution = resolved_character_ids is not None
    requested_character_ids = list(dict.fromkeys(
        str(item or "").strip()
        for item in (resolved_character_ids or [])
        if str(item or "").strip()
    ))

    characters = []
    if requested_character_ids:
        characters = characters_from_public_ids(requested_character_ids)
        loaded_ids = {
            str(item.get("key") or "").strip()
            for item in characters
            if str(item.get("key") or "").strip()
        }
        missing_ids = [item for item in requested_character_ids if item not in loaded_ids]
        if missing_ids:
            raise RuntimeError("人物选择结果无效，请重新选择后再提交。")
        for item in characters:
            item["character_tag_source"] = "character_registry"
            item["match_stage"] = "resolved"
    if not characters and not explicit_resolution:
        characters = find_character_after_translation(original_text, refined_prompt, limit=3)
    # 去重：同一人物多数据源合并
    characters = _dedupe_characters_by_identity(characters)
    if len(characters) > 1:
        # 多个不同人物 → 按多人图处理，不报 ambiguous 错误
        # 注意：这里不限制只选一人，由 enforce_character_preferences 处理多人
        pass
    if characters:
        # 多人图时也剥离每个匹配人物的错误翻译名
        if any(c.get("match_stage") == "original" for c in characters):
            for c in characters:
                if c.get("match_stage") == "original":
                    refined_prompt = _strip_mistranslated_character_names(refined_prompt, c)
        enforced = enforce_character_preferences(
            characters=characters,
            workflow_key="",
            positive_prompt=refined_prompt,
            loras=[],
            request_text=original_text,
        )
        final_prompt = str(enforced.get("positive_prompt") or "").strip()
        try:
            char_source = str((characters[0] if characters else {}).get("character_tag_source") or "")
            if char_source != "agent_fallback":
                if len(characters) > 1:
                    from .smart_agent.character_preferences import validate_multi_character_prompt
                    validate_multi_character_prompt(
                        prompt=final_prompt,
                        characters=characters,
                        workflow_key=str(enforced.get("workflow_key") or ""),
                        loras=list(enforced.get("loras") or []),
                        user_text=original_text,
                    )
                else:
                    validate_character_prompt(
                        prompt=final_prompt,
                        character=characters[0] if characters else None,
                        workflow_key=str(enforced.get("workflow_key") or ""),
                        loras=list(enforced.get("loras") or []),
                        user_text=original_text,
                        all_characters=characters,
                    )
        except CharacterPromptValidationError as exc:
            user_msg = "人物提示词整理失败，请重新尝试；如果该人物不在人物库中，系统将使用翻译后的名称继续生成。"
            raise RuntimeError(user_msg) from exc
        return final_prompt[:MAX_REFINED_PROMPT_CHARS]
    cleaned, _, _ = sanitize_inferred_appearance_tags(
        refined_prompt,
        user_text=original_text,
        character=None,
    )
    return cleaned[:MAX_REFINED_PROMPT_CHARS]


async def _refine_prompt_ollama(settings: Settings, text: str) -> str:
    url = settings.agent_base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": settings.agent_model,
        "messages": [
            {"role": "system", "content": system_prompt_for_settings(settings)},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "keep_alive": settings.agent_keep_alive,
        "options": {
            "temperature": 0.35,
        },
    }
    timeout = max(int(settings.agent_timeout_seconds), 10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post_with_retries(client, url, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Agent 返回 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Agent 返回格式不兼容 Ollama /api/chat") from exc
        try:
            content = data["message"]["content"].strip()
        except Exception as exc:
            raise RuntimeError("Agent 返回格式不兼容 Ollama /api/chat") from exc
        result = clean_agent_prompt(content)
        await _wait_ollama_unloaded(client, settings)
        return result


async def _wait_ollama_unloaded(client: httpx.AsyncClient, settings: Settings) -> None:
    ps_url = settings.agent_base_url.rstrip("/") + "/api/ps"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            response = await client.get(ps_url)
            if response.status_code == 200:
                data = response.json()
                models = data.get("models") or []
                if not any(item.get("name") == settings.agent_model for item in models):
                    return
        except Exception:
            return
        await asyncio.sleep(0.5)


async def _refine_prompt_openai_compatible(settings: Settings, text: str) -> str:
    url = settings.agent_base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.agent_api_key:
        headers["Authorization"] = f"Bearer {settings.agent_api_key}"
    payload = {
        "model": settings.agent_model,
        "temperature": 0.35,
        "messages": [
            {
                "role": "system",
                "content": system_prompt_for_settings(settings),
            },
            {"role": "user", "content": text},
        ],
    }
    timeout = max(int(settings.agent_timeout_seconds), 10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _post_with_retries(client, url, headers=headers, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"Agent 返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Agent 返回格式不兼容 OpenAI chat/completions") from exc
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise RuntimeError("Agent 返回格式不兼容 OpenAI chat/completions") from exc
    return clean_agent_prompt(content)
