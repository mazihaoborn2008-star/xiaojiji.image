from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

DYNAMIC_WORKFLOW_PREFIX = "wf_"

# Smart Agent 默认通用工作流的文件名（不含 .json）
SMART_AGENT_DEFAULT_WORKFLOW_STEM = "Agent用"


def normalize_workflow_name(value: str) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\.(json|workflow)$", "", text, flags=re.I)
    text = re.sub(r"[_\\/\-()\[\]{}（）【】「」『』·・:：,，.。+]+", " ", text)
    return " ".join(text.lower().split())


def dynamic_workflow_key_for_name(name: str) -> str:
    digest = hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:12]
    return f"{DYNAMIC_WORKFLOW_PREFIX}{digest}"


def is_dynamic_workflow_key(key: str) -> bool:
    return str(key or "").startswith(DYNAMIC_WORKFLOW_PREFIX)


def list_dynamic_workflows(workflow_dir: str | Path) -> list[dict[str, Any]]:
    return _list_dynamic_workflows_cached(str(Path(workflow_dir)))


def refresh_dynamic_workflows() -> None:
    _list_dynamic_workflows_cached.cache_clear()


@lru_cache(maxsize=8)
def _list_dynamic_workflows_cached(workflow_dir: str) -> list[dict[str, Any]]:
    root = Path(workflow_dir)
    if not root.exists() or not root.is_dir():
        return []

    workflows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name.lower()):
        try:
            resolved = path.resolve()
            if root.resolve() not in resolved.parents:
                continue
        except OSError:
            continue

        stem = path.stem.strip()
        if not stem:
            continue
        key = dynamic_workflow_key_for_name(stem)
        normalized = normalize_workflow_name(stem)
        category = _classify_workflow(normalized)
        output_count, lora_count, health_status = _inspect_workflow(path)
        workflows.append(
            {
                "key": key,
                "label": _public_label(category),
                "output_count": output_count,
                "notes": _public_notes(category, output_count),
                "category": category,
                "dynamic": True,
                "source": "comfyui_workflow_dir",
                "aliases": _aliases_for_stem(stem),
                "selection_tags": _selection_tags(category, normalized),
                "allow_external_lora": False,
                "preferred": category == "generic",
                "embedded_lora_count": lora_count,
                "health_status": health_status,
                "smart_agent_default": key == SMART_AGENT_DEFAULT_WORKFLOW_KEY,
            }
        )
    return workflows


def _classify_workflow(normalized: str) -> str:
    if any(term in normalized for term in ("video", "视频", "首尾帧", "帧", "wan", "ltx", "framepack")):
        return "video"
    if any(term in normalized for term in ("in game", "ingame", "游戏")):
        return "in_game"
    if any(term in normalized for term in ("图生图", "重绘", "img2img")):
        return "img2img"
    if any(term in normalized for term in ("文生图", "txt2img")):
        return "txt2img"
    if any(term in normalized for term in ("调试", "调流", "debug")):
        return "debug"
    if normalized == "anima" or "anima" in normalized:
        return "generic"
    # Smart Agent 默认通用工作流（Agent用）也归类为 generic
    if "agent" in normalized and ("yong" in normalized or "用" in normalized):
        return "generic"
    return "character_or_specialized"


def _public_label(category: str) -> str:
    labels = {
        "video": "类型工作流",
        "in_game": "In Game 类型工作流",
        "img2img": "图生图类型工作流",
        "txt2img": "文生图类型工作流",
        "debug": "调试类型工作流",
        "generic": "通用工作流",
        "character_or_specialized": "人物/专用工作流",
    }
    return labels.get(category, "专用工作流")


def _public_notes(category: str, output_count: int) -> str:
    details = {
        "video": "scene/type workflow for video or frame-based requests",
        "in_game": "scene/type workflow for in-game style requests",
        "img2img": "scene/type workflow for image-to-image requests",
        "txt2img": "scene/type workflow for text-to-image requests",
        "debug": "scene/type workflow for debug or test requests",
        "generic": "general-purpose workflow from the ComfyUI workflow directory",
        "character_or_specialized": "character-specific or specialized workflow from the ComfyUI workflow directory",
    }
    return f"{details.get(category, 'workflow from the ComfyUI workflow directory')}; output_count={max(1, output_count)}"


def _selection_tags(category: str, normalized: str) -> list[str]:
    tags = [category]
    for term in ("video", "视频", "首尾帧", "in game", "文生图", "图生图", "调流", "调试"):
        if term in normalized:
            tags.append(term)
    return tags


def _aliases_for_stem(stem: str) -> list[str]:
    aliases = [stem]
    normalized = normalize_workflow_name(stem)
    if normalized and normalized not in aliases:
        aliases.append(normalized)
    compact = normalized.replace(" ", "")
    if compact and compact not in aliases:
        aliases.append(compact)
    cleaned = re.sub(r"\b(in game|ingame|workflow|txt2img|img2img)\b", " ", normalized, flags=re.I)
    cleaned = " ".join(cleaned.split())
    if cleaned and cleaned not in aliases:
        aliases.append(cleaned)
    return aliases


def _inspect_workflow(path: Path) -> tuple[int, int, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 1, 0, "invalid_json"
    if not isinstance(data, dict):
        return 1, 0, "invalid_schema"
    if isinstance(data.get("nodes"), list):
        save_count = 0
        lora_count = 0
        for node in data.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("type") or node.get("class_type") or "")
            if class_type == "SaveImage":
                save_count += 1
            if "lora" in class_type.lower():
                lora_count += 1
        return max(1, save_count), lora_count, "ok" if save_count else "no_save_image"
    save_count = 0
    lora_count = 0
    for node in data.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type == "SaveImage":
            save_count += 1
        if "lora" in class_type.lower():
            lora_count += 1
    return max(1, save_count), lora_count, "ok" if save_count else "no_save_image"


# 在模块末尾计算，确保 dynamic_workflow_key_for_name 已定义
SMART_AGENT_DEFAULT_WORKFLOW_KEY = dynamic_workflow_key_for_name(SMART_AGENT_DEFAULT_WORKFLOW_STEM)
