"""Workflow template service for Web API generation workflows.

Handles:
- Workflow definition registry (style_key → file path + node mapping)
- Safe template loading with deepcopy
- Prompt injection (per-node, not generic append)
- Width/height injection
- Seed injection
- LoRA weight injection
- Output node validation
"""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

# ── Workflow definitions ────────────────────────────────────────────

_WEB_WORKFLOW_DIR = Path(r"D:\ComfyUI-aki-v3\ComfyUI\user\default\workflows")

WORKFLOW_DEFINITIONS: dict[str, dict[str, Any]] = {
    "artist_chain_available": {
        "label_zh": "画师串 available",
        "label_en": "Artist Chain Available",
        "file_path": _WEB_WORKFLOW_DIR / "Web 画师串available.json",
        "prompt_node": "18",
        "prompt_field": "text",
        "fixed_trigger_node": None,  # no fixed trigger word node
        "width_node": "7",
        "height_node": "7",
        "seed_node": "9",
        "lora_node": "29",
        "output_node": "27",
        "display_order": 30,
        "enabled": True,
    },
    "morialuluka": {
        "label_zh": "morialuluka",
        "label_en": "morialuluka",
        "file_path": _WEB_WORKFLOW_DIR / "Web morialuluka.json",
        "prompt_node": "250",
        "prompt_field": "value",
        "fixed_trigger_node": "248",
        "width_node": "129",
        "height_node": "129",
        "seed_node": "132",
        "lora_node": "35",
        "output_node": "252",
        "display_order": 31,
        "enabled": True,
    },
    "bridge_complete": {
        "label_zh": "大桥竣工",
        "label_en": "Bridge Complete",
        "file_path": _WEB_WORKFLOW_DIR / "Web 大桥竣工.json",
        "prompt_node": "250",
        "prompt_field": "value",
        "fixed_trigger_node": "248",
        "width_node": "129",
        "height_node": "129",
        "seed_node": "132",
        "lora_node": "35",
        "output_node": "252",
        "display_order": 32,
        "enabled": True,
    },
    "hayakawa_tazuna": {
        "label_zh": "手纲",
        "label_en": "Hayakawa Tazuna",
        "file_path": _WEB_WORKFLOW_DIR / "Web 手纲.json",
        "prompt_node": "250",
        "prompt_field": "value",
        "fixed_trigger_node": "248",
        "width_node": "129",
        "height_node": "129",
        "seed_node": "132",
        "lora_node": "35",
        "output_node": "252",
        "display_order": 33,
        "enabled": True,
    },
    "akikawa_yayoi": {
        "label_zh": "理事长",
        "label_en": "Akikawa Yayoi",
        "file_path": _WEB_WORKFLOW_DIR / "Web 理事长.json",
        "prompt_node": "250",
        "prompt_field": "value",
        "fixed_trigger_node": "248",
        "width_node": "129",
        "height_node": "129",
        "seed_node": "132",
        "lora_node": "35",
        "output_node": "252",
        "display_order": 34,
        "enabled": True,
    },
}

# Public alias for external consumers
WEB_WORKFLOW_DIR = _WEB_WORKFLOW_DIR

# Set of valid style_keys for quick lookup
VALID_STYLE_KEYS: set[str] = {k for k, v in WORKFLOW_DEFINITIONS.items() if v["enabled"]}


def get_workflow_definition(style_key: str) -> dict[str, Any] | None:
    """Get workflow definition by style_key. Returns None if not found or disabled."""
    defn = WORKFLOW_DEFINITIONS.get(style_key)
    if defn and defn["enabled"]:
        return defn
    return None


def is_web_api_workflow(style_key: str) -> bool:
    """Check if style_key is one of the 5 Web API workflows."""
    return style_key in VALID_STYLE_KEYS


def load_workflow_template(style_key: str) -> dict[str, Any]:
    """Load workflow JSON template from disk. Returns a fresh dict each time.

    Raises FileNotFoundError if the template file is missing.
    Raises json.JSONDecodeError if the template is invalid JSON.
    """
    defn = WORKFLOW_DEFINITIONS.get(style_key)
    if not defn:
        raise ValueError(f"Unknown workflow style_key: {style_key}")
    path = defn["file_path"]
    if not path.exists():
        raise FileNotFoundError(f"Workflow template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Workflow template is not a dict: {path}")
    return data


def prepare_workflow_payload(
    style_key: str,
    canonical_prompt: str,
    width: int,
    height: int,
    seed: int | None = None,
    lora_weight: float = 1.0,
) -> dict[str, Any]:
    """Prepare a complete ComfyUI API workflow payload.

    1. Loads template from disk (fresh each time)
    deepcopy is implicit since we load from file each call.
    2. Injects prompt into the correct node
    3. Injects width/height
    4. Injects random seed
    5. Injects LoRA weight
    6. Returns the ready-to-POST workflow dict

    The caller should NOT modify the returned dict's template on disk.
    """
    defn = WORKFLOW_DEFINITIONS.get(style_key)
    if not defn:
        raise ValueError(f"Unknown workflow style_key: {style_key}")

    # Load fresh template from disk (no caching → no cross-task pollution)
    workflow = load_workflow_template(style_key)

    # ── Prompt injection ──
    prompt_node = defn["prompt_node"]
    prompt_field = defn["prompt_field"]
    if prompt_node not in workflow:
        raise ValueError(f"Prompt node {prompt_node} not found in workflow {style_key}")
    workflow[prompt_node]["inputs"][prompt_field] = canonical_prompt

    # ── Fixed trigger word preservation check ──
    fixed_node = defn.get("fixed_trigger_node")
    if fixed_node and fixed_node in workflow:
        # Verify fixed trigger node is NOT overwritten
        # It should still have its original value from the template
        pass  # We never touch the fixed trigger node

    # ── Width/Height injection ──
    width_node = defn["width_node"]
    height_node = defn["height_node"]
    if width_node in workflow:
        workflow[width_node]["inputs"]["width"] = int(width)
    if height_node in workflow:
        workflow[height_node]["inputs"]["height"] = int(height)

    # ── Seed injection ──
    if seed is None:
        seed = random.randint(1, 999999999999999)
    seed_node = defn["seed_node"]
    if seed_node in workflow:
        workflow[seed_node]["inputs"]["seed"] = int(seed)

    # ── LoRA weight injection ──
    lora_node = defn["lora_node"]
    if lora_node in workflow:
        lora_inputs = workflow[lora_node]["inputs"]
        lora_inputs["strength_model"] = float(lora_weight)
        lora_inputs["strength_clip"] = float(lora_weight)

    return workflow


def validate_workflow_output(workflow: dict[str, Any], style_key: str) -> bool:
    """Validate that the workflow has the expected SaveImage output node."""
    defn = WORKFLOW_DEFINITIONS.get(style_key)
    if not defn:
        return False
    output_node = defn["output_node"]
    if output_node not in workflow:
        return False
    node = workflow[output_node]
    if node.get("class_type") != "SaveImage":
        return False
    return True


def get_all_workflow_info() -> list[dict[str, Any]]:
    """Return info about all registered Web API workflows for debugging."""
    result = []
    for key, defn in WORKFLOW_DEFINITIONS.items():
        result.append({
            "key": key,
            "label_zh": defn["label_zh"],
            "label_en": defn["label_en"],
            "file_path": str(defn["file_path"]),
            "prompt_node": defn["prompt_node"],
            "prompt_field": defn["prompt_field"],
            "fixed_trigger_node": defn.get("fixed_trigger_node"),
            "width_node": defn["width_node"],
            "height_node": defn["height_node"],
            "seed_node": defn["seed_node"],
            "lora_node": defn["lora_node"],
            "output_node": defn["output_node"],
            "display_order": defn["display_order"],
            "enabled": defn["enabled"],
            "file_exists": defn["file_path"].exists(),
        })
    return result
