from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dynamic_workflows import is_dynamic_workflow_key

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "lora_registry.json"


def list_loras() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw if isinstance(raw, list) else raw.get("items", [])
    clean: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        clean.append(
            {
                "key": key,
                "label": str(item.get("label") or key),
                "tags": str(item.get("tags") or ""),
                "compatible_workflows": list(item.get("compatible_workflows") or []),
                "min_weight": float(item.get("min_weight", 0.0)),
                "max_weight": float(item.get("max_weight", 1.5)),
                "default_weight": float(item.get("default_weight", 1.0)),
                "comfy_lora_name": str(item.get("comfy_lora_name") or ""),
                "preferred": bool(item.get("preferred", False)),
            }
        )
    return clean


def get_lora(key: str) -> dict[str, Any] | None:
    key = str(key or "").strip()
    if not key:
        return None
    for item in list_loras():
        if item["key"] == key:
            return item
    return None


def lora_summaries() -> str:
    items = list_loras()
    if not items:
        return "- none: no extra LoRA is exposed by the server whitelist"
    return "\n".join(
        f"- {item['key']}: {item['label']}; tags={item['tags']}; compatible={','.join(item['compatible_workflows']) or 'any'}"
        for item in items
    )


def sanitize_loras(raw: Any, workflow_key: str) -> list[dict[str, Any]]:
    registry = {item["key"]: item for item in list_loras()}
    if not isinstance(raw, list):
        return []
    selected: list[dict[str, Any]] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        entry = registry.get(key)
        if not entry:
            continue
        compatible = entry.get("compatible_workflows") or []
        if compatible and workflow_key not in compatible and not is_dynamic_workflow_key(workflow_key):
            continue
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        weight = max(float(entry["min_weight"]), min(float(entry["max_weight"]), weight))
        selected.append({"key": key, "weight": round(weight, 3)})
    return selected
