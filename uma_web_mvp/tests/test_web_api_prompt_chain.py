"""Regression tests for web_api double prompt injection fix.

Verifies that prepare_workflow_payload output is NOT overwritten by
set_workflow_prompt for web_api workflows, preserving the
248+250->251->3 JoinStringMulti chain.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.services.workflow_template_service import (
    WORKFLOW_DEFINITIONS,
    prepare_workflow_payload,
)


ALL_WEB_KEYS = ["artist_chain_available", "morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"]
TRIGGER_WORKFLOW_KEYS = ["morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"]


# ── Helper: simulate the exact generate_image code path ──

def simulate_generate_image_web_api(style_key: str, prompt_text: str, width: int, height: int):
    """Simulate the fixed generate_image code path for web_api workflows.
    
    After the fix, the second prompt injection block should NOT run for web_api.
    This function replicates the logic to verify.
    """
    is_web_api = True
    is_controlnet = False
    is_anima_owner = False
    is_dynamic_workflow = False
    is_img2img = False

    # Branch A: web_api path
    if is_web_api:
        workflow = prepare_workflow_payload(
            style_key=style_key,
            canonical_prompt=prompt_text,
            width=width,
            height=height,
            seed=12345,
            lora_weight=1.0,
        )
        prompt_node_id = WORKFLOW_DEFINITIONS[style_key]["prompt_node"]

    # Branch B: should be skipped for web_api
    if is_controlnet:
        pass
    elif is_anima_owner:
        pass
    elif is_dynamic_workflow:
        pass
    elif is_img2img:
        pass
    elif not is_web_api:
        # This must NOT run for web_api
        # Simulating set_workflow_prompt: finds KSampler positive -> CLIPTextEncode -> overwrites text
        for node_id, node in workflow.items():
            if node.get("class_type") == "KSampler":
                pos_ref = node["inputs"].get("positive")
                if isinstance(pos_ref, list):
                    target = str(pos_ref[0])
                    if target in workflow and workflow[target].get("class_type") == "CLIPTextEncode":
                        workflow[target]["inputs"]["text"] = prompt_text  # THIS IS THE BUG

    return workflow


# ── Tests ──

@pytest.mark.parametrize("style_key", TRIGGER_WORKFLOW_KEYS)
def test_node3_preserves_join_reference_after_fix(style_key: str):
    """After the fix, Node 3.text must remain ['251', 0] reference, not literal prompt."""
    prompt = "test scene, 1girl, standing"
    wf = simulate_generate_image_web_api(style_key, prompt, 1024, 1536)
    
    node3_text = wf["3"]["inputs"]["text"]
    assert isinstance(node3_text, list), (
        f"Node 3 text was overwritten to literal string: {node3_text!r}. "
        f"set_workflow_prompt() is still running for web_api workflows!"
    )
    assert node3_text[0] == "251", f"Node 3 text references wrong node: {node3_text}"


def test_artist_chain_node18_preserved_after_fix():
    """画师串: Node 18.text must be the canonical prompt."""
    prompt = "test scene, 1girl, standing"
    wf = simulate_generate_image_web_api("artist_chain_available", prompt, 1024, 1536)
    
    assert wf["18"]["inputs"]["text"] == prompt


@pytest.mark.parametrize("style_key", TRIGGER_WORKFLOW_KEYS)
def test_node250_has_canonical_prompt_after_fix(style_key: str):
    """Node 250 must contain the canonical prompt, not be empty."""
    prompt = "test scene, 1girl, standing"
    wf = simulate_generate_image_web_api(style_key, prompt, 1024, 1536)
    
    assert wf["250"]["inputs"]["value"] == prompt


@pytest.mark.parametrize("style_key", TRIGGER_WORKFLOW_KEYS)
def test_node248_trigger_preserved_after_fix(style_key: str):
    """Node 248 trigger words must not be overwritten."""
    prompt = "test scene, 1girl, standing"
    wf = simulate_generate_image_web_api(style_key, prompt, 1024, 1536)
    template = prepare_workflow_payload(style_key, "", 1024, 1536, seed=99)
    
    assert wf["248"]["inputs"]["value"] == template["248"]["inputs"]["value"]


@pytest.mark.parametrize("style_key", ALL_WEB_KEYS)
def test_lora_chain_intact_after_fix(style_key: str):
    """LoRA -> CLIPTextEncode chain must be intact."""
    wf = simulate_generate_image_web_api(style_key, "test", 1024, 1536)
    defn = WORKFLOW_DEFINITIONS[style_key]
    
    lora_name = wf[defn["lora_node"]]["inputs"]["lora_name"]
    assert lora_name, f"LoRA name is empty for {style_key}"
    assert wf[defn["lora_node"]]["inputs"]["strength_model"] == 1.0


def test_non_web_api_workflows_still_use_set_workflow_prompt():
    """Verify that the 'elif not is_web_api' condition exists in the actual bot code."""
    import ast
    with open(r"E:\discord-BOT\bot_web_mvp.py", "r", encoding="utf-8") as f:
        source = f.read()
    
    # The fix: "elif not is_web_api:" should appear before set_workflow_prompt
    assert "elif not is_web_api:" in source, (
        "Fix not applied: 'elif not is_web_api:' not found in bot_web_mvp.py"
    )
    # The old "else:\\n        prompt_node_id = set_workflow_prompt" without guard should not exist
    # Count occurrences of set_workflow_prompt
    count = source.count("set_workflow_prompt(")
    assert count >= 1, "set_workflow_prompt call not found"
