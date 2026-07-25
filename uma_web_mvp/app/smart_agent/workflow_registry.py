from __future__ import annotations

from typing import Any

from app.catalog import STYLES
from app.config import get_settings

from .dynamic_workflows import is_dynamic_workflow_key, list_dynamic_workflows, refresh_dynamic_workflows


IMAGE_ONLY_DISABLED = {"anima", "controlnet", "anima_owner"}
OUTPUT_COUNTS = {
    "anima_owner": 2,
}


def list_workflows(*, is_admin: bool = False) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    for style in STYLES:
        key = str(style.get("key") or "")
        if not key or key in IMAGE_ONLY_DISABLED:
            continue
        if "txt2img" not in style.get("modes", []):
            continue
        if style.get("hidden"):
            continue
        if style.get("owner_only") and not is_admin:
            continue
        workflows.append(
            {
                "key": key,
                "label": str(style.get("name") or key),
                "output_count": int(OUTPUT_COUNTS.get(key, 1)),
                "notes": _workflow_notes(key),
                "character_key": str(style.get("character_key") or ""),
                "aliases": list(style.get("aliases") or []),
                "selection_tags": list(style.get("selection_tags") or []),
                "allow_external_lora": bool(style.get("allow_external_lora", False)),
                "preferred": bool(style.get("preferred", False)),
            }
        )
    settings = get_settings()
    workflows.extend(list_dynamic_workflows(settings.comfyui_workflow_directory))
    return workflows


def get_workflow(key: str, *, is_admin: bool = False) -> dict[str, Any] | None:
    key = (key or "").strip()
    for item in list_workflows(is_admin=is_admin):
        if item["key"] == key:
            return item
    return None


def workflow_summaries(*, is_admin: bool = False) -> str:
    return "\n".join(
        f"- {item['key']}: {item['label']}; output_count={item['output_count']}; {item['notes']}"
        for item in list_workflows(is_admin=is_admin)
    )


def warm_workflow_index() -> int:
    refresh_dynamic_workflows()
    settings = get_settings()
    return len(list_dynamic_workflows(settings.comfyui_workflow_directory))


def workflow_selection_label(workflow_key: str) -> str:
    workflow = get_workflow(workflow_key)
    if not workflow:
        return "通用 workflow"
    if workflow.get("smart_agent_default"):
        return "Smart Agent 默认工作流"
    if workflow.get("dynamic"):
        category = str(workflow.get("category") or "")
        if category == "character_or_specialized":
            return "人物专属工作流"
        if category in {"video", "in_game", "img2img", "txt2img", "debug"}:
            return "类型工作流"
        return "目录工作流"
    return "通用 workflow"


def _workflow_notes(key: str) -> str:
    mapping = {
        "style_a": "general anime illustration, balanced default style",
        "style_b": "soft watercolor-like anime illustration",
        "anima_owner": "recommended high-quality Anima double sample, image-only, two outputs",
        "artist_chain_available": "artist chain workflow, prompt overwrites node 18",
        "morialuluka": "morialuluka character workflow, fixed trigger word + user prompt",
        "bridge_complete": "bridge complete character workflow, fixed trigger word + user prompt",
        "hayakawa_tazuna": "hayakawa tazuna character workflow, fixed trigger word + user prompt",
        "akikawa_yayoi": "akikawa yayoi character workflow, fixed trigger word + user prompt",
        "loves_only_you": "Uma Musume character workflow",
        "red_goddess": "Uma Musume character workflow",
        "blue_goddess": "Uma Musume character workflow",
        "b95": "Uma Musume character workflow",
        "haiseiko": "Uma Musume character workflow",
        "mihono_bourbon": "Uma Musume character workflow",
        "wonder_acute": "Uma Musume character workflow",
        "verxina": "Uma Musume character workflow",
        "sakura_chiyono_o": "Uma Musume character workflow",
        "daiichi_ruby": "Uma Musume character workflow",
        "copano_rickey": "Uma Musume character workflow",
    }
    return mapping.get(key, "image txt2img workflow")
