from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from app.config import Settings

from .disambiguation_engine import (
    all_groups_resolved,
    analyze_user_request,
    characters_from_public_ids,
    create_pending_disambiguation_json,
    is_disambiguation_choice,
    is_new_generation_request,
    is_scene_supplement,
    resolve_group,
)
from .v2_client import (
    chat_with_agent_v2,
    has_visual_plan,
    infer_external_character,
    to_legacy_prompt_fields,
)
from .v2_protocol import (
    character_display,
    prepare_turn,
    resolve_character_operation_v2,
    safe_previous_state,
    safe_recent_messages,
    sanitize_public_text,
)
from .v2_store import (
    finish_turn_for_message,
    get_message_resolution,
    mark_turn_processing,
)


_VISUAL_WORDS = {
    "场景", "背景", "服装", "衣服", "动作", "表情", "构图", "镜头", "光线", "氛围",
    "公园", "教室", "卧室", "海边", "沙滩", "城市", "街道", "夜晚", "白天", "黄昏",
    "生成", "生图", "出图", "图片", "画面", "帮我", "给我", "一个", "一张",
}


def _probable_character_name(value: str) -> bool:
    clean = str(value or "").strip(" ，,。.!！?？")
    if not clean or len(clean) > 32:
        return False
    if any(word in clean for word in _VISUAL_WORDS):
        return False
    # A name needs at least two CJK chars, or a normal booru/foreign token.
    cjk_count = sum(1 for ch in clean if "\u3400" <= ch <= "\u9fff")
    if cjk_count >= 2:
        return True
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_ ()'\-]{2,80}", clean))


def _selected_json_from_draft(runtime: Any, draft: dict[str, Any] | None) -> str:
    if not draft:
        return ""
    try:
        plan = json.loads(str(draft.get("plan_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(plan, dict):
        return ""
    return str(plan.get("selected_characters_json") or "")


def _character_ids_from_pending(pending: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in pending.get("groups", []) or []:
        selected = str(group.get("selected_identity_key") or "").strip()
        if selected and selected not in seen:
            seen.add(selected)
            result.append(selected)
    return result


def _pending_supplements(pending: dict[str, Any]) -> list[str]:
    constraints = pending.get("constraints") or {}
    values = constraints.get("supplements") if isinstance(constraints, dict) else []
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()][-5:]


def _structured_draft_json(result: dict[str, Any]) -> str:
    payload = {
        "scene": str(result.get("scene") or "").strip(),
        "style": str(result.get("style") or "").strip(),
        "clothing": str(result.get("clothing") or "").strip(),
        "expression": str(result.get("expression") or "").strip(),
        "action": str(result.get("pose_action") or result.get("action") or "").strip(),
        "composition": str(result.get("composition") or "").strip(),
        "lighting": str(result.get("lighting") or "").strip(),
        "mood": str(result.get("mood") or "").strip(),
        "resolution_hint": str(result.get("resolution_hint") or "").strip(),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def process_smart_agent_turn_v2(
    *,
    s: Settings,
    conversation_code: str,
    conversation_id: int,
    legacy_id: str,
    username: str,
    user_public_id: str,
    user_msg: str,
    resolved_intent: str,
    client_request_id: str | None,
    message_id: int | None = None,
) -> None:
    """Process exactly one user turn.

    The LLM may plan and converse, but character identity, task creation,
    billing, queue state and public execution messages remain server-owned.
    """
    # Lazy import avoids a module-import cycle.  app.main is fully initialized
    # before this function runs in the background worker.
    from app import main as runtime

    request_id = hashlib.sha256(
        f"v2:{conversation_code}:{client_request_id or ''}:{message_id or 0}".encode("utf-8")
    ).hexdigest()[:12]
    message_terminal = False
    turn_terminal_status = "completed"
    final_error = ""

    try:
        if message_id:
            runtime.mark_smart_agent_message_status(s, message_id=message_id, status="processing")
            mark_turn_processing(s, message_id=message_id)
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="message_processing",
                public_message="正在理解你的需求……",
            )

        runtime._smart_trace(
            "v2_turn_started",
            request_id=request_id,
            conversation_code=conversation_code,
            resolved_intent=resolved_intent,
        )

        conv = runtime.get_conversation_by_code(s, conversation_code=conversation_code)
        if not conv:
            final_error = "conversation_not_found"
            turn_terminal_status = "failed"
            return

        recent = runtime.get_conversation_messages(s, conversation_id=conversation_id, limit=20)
        memory = str(conv.get("memory_summary") or "")
        draft = await asyncio.to_thread(
            runtime.get_smart_agent_prompt_draft,
            s,
            conversation_id=conversation_id,
        )
        turn = prepare_turn(
            user_msg,
            resolved_intent=resolved_intent,
            client_request_id=client_request_id,
            message_id=message_id,
        )

        exact_character_ids = (
            get_message_resolution(s, message_id=message_id) if message_id else []
        )
        pending = runtime.get_pending_disambiguation_json(s, conversation_id=conversation_id)

        # -------- Resolve an existing ambiguity before any new character search.
        if pending and not exact_character_ids:
            all_candidates: list[dict[str, Any]] = []
            for group in pending.get("groups", []) or []:
                if group.get("status") == "pending":
                    all_candidates.extend(group.get("candidates", []) or [])

            if is_disambiguation_choice(user_msg, all_candidates):
                for group in pending.get("groups", []) or []:
                    if group.get("status") == "resolved":
                        continue
                    matched = runtime.resolve_character_from_candidates(
                        group.get("candidates", []) or [], user_msg
                    )
                    if matched:
                        identity = str(
                            matched.get("identity_key")
                            or matched.get("character_key")
                            or ""
                        ).strip()
                        if identity:
                            resolve_group(pending, str(group.get("group_id") or ""), identity)

                if not all_groups_resolved(pending):
                    runtime.save_pending_disambiguation_json(
                        s,
                        conversation_id=conversation_id,
                        disambiguation_json=pending,
                    )
                    runtime._reemit_disambiguation_cards(s, conversation_id, pending)
                    turn_terminal_status = "awaiting_user"
                    return

                exact_character_ids = _character_ids_from_pending(pending)
                original_request = str(pending.get("original_request") or "").strip()
                supplements = _pending_supplements(pending)
                user_msg = "\n".join(
                    item for item in (original_request, *supplements) if item
                ) or user_msg
                turn = prepare_turn(
                    user_msg,
                    resolved_intent="generate",
                    client_request_id=client_request_id,
                    message_id=message_id,
                )
                runtime.clear_pending_disambiguation(s, conversation_id=conversation_id)
                names = [
                    str(item.get("name_zh") or item.get("name_en") or "")
                    for item in all_candidates
                    if str(item.get("identity_key") or item.get("character_key") or "")
                    in set(exact_character_ids)
                ]
                confirmation = "已确定人物"
                if names:
                    confirmation += "：" + "、".join(dict.fromkeys(names))
                confirmation += "。正在继续整理画面。"
                runtime.add_conversation_message(
                    s,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=confirmation,
                    safe_content=confirmation,
                )
                runtime._add_safe_smart_agent_event(
                    s,
                    conversation_id=conversation_id,
                    event_type="assistant_message",
                    public_message=confirmation,
                )
            elif is_scene_supplement(user_msg):
                constraints = pending.get("constraints") or {}
                if not isinstance(constraints, dict):
                    constraints = {}
                supplements = constraints.get("supplements") or []
                if not isinstance(supplements, list):
                    supplements = []
                supplements.append(user_msg)
                constraints["supplements"] = supplements[-5:]
                pending["constraints"] = constraints
                runtime.save_pending_disambiguation_json(
                    s,
                    conversation_id=conversation_id,
                    disambiguation_json=pending,
                )
                reply = "补充要求已记录。请先选择具体人物。"
                runtime.add_conversation_message(
                    s,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=reply,
                    safe_content=reply,
                )
                runtime._add_safe_smart_agent_event(
                    s,
                    conversation_id=conversation_id,
                    event_type="assistant_message",
                    public_message=reply,
                )
                runtime._add_safe_smart_agent_event(
                    s, conversation_id=conversation_id, event_type="done", public_message=""
                )
                turn_terminal_status = "awaiting_user"
                return
            elif is_new_generation_request(user_msg):
                runtime.supersede_pending_disambiguation(s, conversation_id=conversation_id)
                pending = None
            else:
                runtime._reemit_disambiguation_cards(s, conversation_id, pending)
                turn_terminal_status = "awaiting_user"
                return

        # -------- Current selected character state.
        current_selected_json = _selected_json_from_draft(runtime, draft)
        current_characters = runtime._json_to_characters(current_selected_json)
        new_characters: list[dict[str, Any]] = []
        character_tag_source = ""
        translated_character_name = ""
        original_character_name = ""

        if exact_character_ids:
            new_characters = characters_from_public_ids(exact_character_ids)
            for item in new_characters:
                item["character_tag_source"] = "character_registry"
                item["match_stage"] = "confirmed_resolution"
            character_tag_source = "character_registry"
        else:
            character_text = turn.visual_text or turn.raw_text
            analysis = analyze_user_request(character_text)
            if analysis.get("is_ambiguous"):
                pending_payload = create_pending_disambiguation_json(analysis, turn.raw_text)
                runtime.save_pending_disambiguation_json(
                    s,
                    conversation_id=conversation_id,
                    disambiguation_json=pending_payload,
                )
                runtime._emit_disambiguation_event_v2(
                    s, conversation_id=conversation_id, pending=pending_payload
                )
                turn_terminal_status = "awaiting_user"
                return

            resolved_ids = [
                str(
                    item.get("identity_key")
                    or item.get("character_key")
                    or item.get("key")
                    or ""
                ).strip()
                for item in analysis.get("resolved_characters", []) or []
            ]
            resolved_ids = [item for item in resolved_ids if item]
            if resolved_ids:
                new_characters = characters_from_public_ids(resolved_ids)

            if not new_characters:
                direct = runtime._dedupe_character_matches(
                    runtime.find_characters(character_text)
                )
                if len(direct) == 1 and not analysis.get("is_ambiguous"):
                    new_characters = direct

            if not new_characters:
                explicit = runtime._detect_explicit_user_characters(turn.visual_text)
                if explicit:
                    new_characters = explicit
                    character_tag_source = "explicit_user_character"

            if not new_characters:
                candidate = runtime.extract_possible_character_names(turn.visual_text)
                if candidate and _probable_character_name(candidate):
                    original_character_name = candidate
                    translated = await runtime.translate_character_name(candidate)
                    translated_character_name = (
                        str(translated or "").strip() if translated != candidate else ""
                    )
                    effective_name = translated_character_name or original_character_name
                    fallback = runtime.build_agent_fallback_character(
                        effective_name, original_character_name
                    )
                    if fallback:
                        # A library miss remains a user-explicit character.  It is
                        # never re-matched to a different library character after
                        # translation.
                        fallback["character_tag_source"] = "explicit_user_character"
                        fallback["source"] = "explicit_user_character"
                        fallback["explicit_tags"] = [
                            effective_name.strip().lower().replace(" ", "_")
                        ]
                        fallback["canonical_tags"] = list(fallback["explicit_tags"])
                        new_characters = [fallback]
                        character_tag_source = "explicit_user_character"

            if not new_characters:
                inferred = await infer_external_character(s, turn.visual_text)
                if inferred:
                    original_character_name = str(inferred["original_name"])
                    translated_character_name = str(inferred["identity_tag"])
                    fallback = runtime.build_agent_fallback_character(
                        translated_character_name, original_character_name
                    )
                    if fallback:
                        fallback["character_tag_source"] = "explicit_user_character"
                        fallback["source"] = "explicit_user_character"
                        fallback["explicit_tags"] = [translated_character_name]
                        fallback["canonical_tags"] = [translated_character_name]
                        new_characters = [fallback]
                        character_tag_source = "explicit_user_character"

        if new_characters and not character_tag_source:
            character_tag_source = str(
                new_characters[0].get("character_tag_source") or "character_registry"
            )

        operation = resolve_character_operation_v2(
            turn.raw_text,
            has_current=bool(current_characters),
            has_new_characters=bool(new_characters),
        )
        selected_characters_json, characters = runtime._apply_character_operation(
            operation,
            current_selected_json,
            new_characters,
        )
        if not characters and current_characters and operation == "generation_supplement":
            characters = current_characters
            selected_characters_json = current_selected_json

        if operation == "query_characters":
            display = character_display(characters or current_characters)
            if display:
                reply = "当前人物：" + "、".join(
                    item["name"] + (f"（{item['series']}）" if item.get("series") else "")
                    for item in display
                )
            else:
                reply = "当前还没有选择人物。"
            runtime.add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=reply,
                safe_content=reply,
            )
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=reply,
            )
            runtime._add_safe_smart_agent_event(
                s, conversation_id=conversation_id, event_type="done", public_message=""
            )
            return

        runtime._validate_request_policy(s, turn.raw_text)

        previous_state = safe_previous_state(draft)
        model_message = turn.visual_text
        if turn.meta_only:
            if not previous_state:
                reply = "还没有可生成的画面方案。请先告诉我人物、场景或想要的画面。"
                runtime.add_conversation_message(
                    s,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=reply,
                    safe_content=reply,
                )
                runtime._add_safe_smart_agent_event(
                    s,
                    conversation_id=conversation_id,
                    event_type="assistant_message",
                    public_message=reply,
                )
                runtime._add_safe_smart_agent_event(
                    s, conversation_id=conversation_id, event_type="done", public_message=""
                )
                return
            model_message = "沿用当前完整方案，不改变已有视觉要求。"

        if not model_message and characters:
            model_message = "为当前人物准备一张图片。"

        result_v2 = await chat_with_agent_v2(
            s,
            user_message=model_message or turn.raw_text,
            generation_requested=turn.generation_requested,
            previous_state=previous_state,
            selected_characters=character_display(characters),
            recent_messages=safe_recent_messages(recent, limit=8),
        )

        if result_v2.get("provider_fallback"):
            reply = str(result_v2.get("reply") or "智能 Agent 暂时无法连接，请稍后重试。")
            runtime.add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=reply,
                safe_content=reply,
            )
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="error",
                public_message=reply,
                private_detail="agent_v2_provider_unavailable",
            )
            turn_terminal_status = "failed"
            final_error = "agent_v2_provider_unavailable"
            return

        if result_v2.get("next_step") == "clarify" and not has_visual_plan(result_v2):
            question = sanitize_public_text(
                str(result_v2.get("clarification_question") or ""),
                fallback="请再补充一个关键画面要求。",
            )
            runtime.add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=question,
                safe_content=question,
            )
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=question,
            )
            runtime._add_safe_smart_agent_event(
                s, conversation_id=conversation_id, event_type="done", public_message=""
            )
            turn_terminal_status = "awaiting_user"
            return

        legacy_result = to_legacy_prompt_fields(result_v2)
        # Keep lighting separately in the stored structured state.
        legacy_result["lighting"] = result_v2.get("lighting", "")

        effective_request = "\n".join(
            item
            for item in (
                str(previous_state.get("previous_user_request") or "").strip(),
                turn.visual_text,
            )
            if item
        ) or turn.raw_text

        snippets = runtime.search_prompt_snippets(effective_request)
        if not has_visual_plan(result_v2) and not previous_state:
            if characters:
                runtime._save_character_draft(
                    s,
                    conversation_id=conversation_id,
                    selected_characters_json=selected_characters_json,
                    resolved_intent=resolved_intent,
                )
                names = "、".join(item["name"] for item in character_display(characters))
                reply = f"已记录人物 {names}。再告诉我场景、服装、动作或构图。"
            else:
                reply = sanitize_public_text(
                    str(result_v2.get("reply") or ""),
                    fallback="请描述想生成的角色或场景。",
                )
            runtime.add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=reply,
                safe_content=reply,
            )
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=reply,
            )
            runtime._add_safe_smart_agent_event(
                s, conversation_id=conversation_id, event_type="done", public_message=""
            )
            turn_terminal_status = "awaiting_user"
            return

        if turn.generation_requested:
            for event_type, message in (
                ("building_prompt", "正在整理画面方案……"),
                ("validating_plan", "正在检查人物和生成参数……"),
            ):
                runtime._add_safe_smart_agent_event(
                    s,
                    conversation_id=conversation_id,
                    event_type=event_type,
                    public_message=message,
                )

        workflow_key = runtime.SMART_AGENT_DEFAULT_WORKFLOW_KEY
        if not runtime.get_workflow(workflow_key):
            raise RuntimeError("workflow_not_found")

        resolution_key = runtime._local_resolution_key(legacy_result, effective_request)
        resolution = runtime.get_resolution_or_default(resolution_key)
        positive_prompt = runtime._build_local_positive_prompt(
            result=legacy_result,
            request_text=effective_request,
            characters=characters,
            snippets=snippets,
            translated_character_name=translated_character_name,
            character_tag_source=character_tag_source,
            selected_characters_json=selected_characters_json,
        )[:3000].strip()
        if not positive_prompt:
            raise RuntimeError("empty_prompt")

        loras = runtime.sanitize_loras([], workflow_key)
        enforced = runtime.enforce_character_preferences(
            characters=characters,
            workflow_key=workflow_key,
            positive_prompt=positive_prompt,
            loras=loras,
            is_admin=str(legacy_id) == str(s.owner_user_id),
            request_text=effective_request,
        )
        workflow_key = str(enforced["workflow_key"])
        if workflow_key == "anima_owner":
            workflow_key = runtime.SMART_AGENT_DEFAULT_WORKFLOW_KEY
        positive_prompt = str(enforced["positive_prompt"])[:3000].strip()
        loras = list(enforced["loras"])

        protected_tags = [
            tag
            for character in runtime._json_to_characters(selected_characters_json)
            for tag in runtime.locked_character_tags(character)
        ]
        positive_prompt, _ = runtime._apply_prompt_core_fidelity(
            positive_prompt,
            effective_request,
            protected_tags=protected_tags,
        )
        resolved_characters = runtime._json_to_characters(selected_characters_json)
        if resolved_characters:
            positive_prompt = runtime._clean_foreign_character_tags_multi(
                positive_prompt, resolved_characters
            )
        positive_prompt, final_core = runtime._apply_prompt_core_fidelity(
            positive_prompt,
            effective_request,
            protected_tags=protected_tags,
        )

        character_key = runtime.stable_character_key(characters[0]) if characters else ""
        fallback_level = str(
            enforced.get("fallback_level") or ("character_tags" if characters else "none")
        )
        workflow_source = (
            fallback_level
            if fallback_level in {
                "character_workflow", "character_lora", "character_tags", "type_workflow"
            }
            else "smart_agent_default"
        )

        if character_tag_source != "explicit_user_character" and character_key:
            runtime.validate_character_prompt(
                prompt=positive_prompt,
                character=characters[0] if characters else None,
                workflow_key=workflow_key,
                loras=loras,
                user_text=effective_request,
            )

        selected_public = runtime.public_character_matches(characters)
        plan_json = json.dumps(
            {
                "version": 2,
                "workflow_key": workflow_key,
                "resolution_key": resolution_key,
                "width": int(resolution["width"]),
                "height": int(resolution["height"]),
                "positive_prompt": positive_prompt,
                "negative_prompt": "",
                "loras": loras,
                "fallback_level": fallback_level,
                "workflow_source": workflow_source,
                "character_key": character_key,
                "selected_characters": selected_public,
                "selected_characters_json": selected_characters_json,
                "conversation_code": conversation_code,
                "conversation_id": conversation_id,
                "character_tag_source": character_tag_source,
                "translated_character_name": translated_character_name,
                "original_character_name": original_character_name,
                "core_fidelity": final_core,
                "turn_id": turn.turn_key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        draft = await asyncio.to_thread(
            runtime.save_smart_agent_prompt_draft,
            s,
            conversation_id=conversation_id,
            message_id=message_id,
            prompt_draft=positive_prompt,
            plan_json=plan_json,
            request_text=effective_request,
            workflow_key=workflow_key,
            loras_json=json.dumps(loras, ensure_ascii=False, separators=(",", ":")),
            prompt_source="smart_agent_v2",
            character_key=character_key,
            workflow_source=workflow_source,
            fallback_level=fallback_level,
            width=int(resolution["width"]),
            height=int(resolution["height"]),
            structured_draft_json=_structured_draft_json(result_v2),
        )

        memory_update = sanitize_public_text(str(result_v2.get("memory_update") or ""))
        if memory_update:
            runtime.update_conversation_summary(
                s,
                conversation_id=conversation_id,
                memory_summary=memory_update,
            )

        if not turn.generation_requested:
            reply = sanitize_public_text(
                str(result_v2.get("reply") or ""),
                fallback="方案已记录。继续告诉我要修改什么，或直接说开始生成。",
            )
            runtime.add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=reply,
                safe_content=reply,
            )
            runtime._add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=reply,
            )
            runtime._add_safe_smart_agent_event(
                s, conversation_id=conversation_id, event_type="done", public_message=""
            )
            return

        confirmation = await asyncio.to_thread(
            runtime.confirm_smart_agent_prompt_draft_atomic,
            s,
            conversation_id=conversation_id,
            user_id=legacy_id,
            username=username,
            job_code=runtime.make_job_code(),
            cost_credits=int(s.smart_agent_cost_credits),
            conversation_code=conversation_code,
            client_request_id=f"smart-v2:{client_request_id or turn.turn_key}",
        )
        job_code = str(confirmation["job_code"])
        runtime._add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="queued",
            job_code=job_code,
            public_message=f"任务已加入队列：{job_code}",
        )
        runtime._add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="generated",
            job_code=job_code,
            public_message="",
        )
        runtime._add_safe_smart_agent_event(
            s, conversation_id=conversation_id, event_type="done", public_message=""
        )
        runtime.redis_delete(
            s,
            "uma:cache:queue_status",
            f"uma:cache:tasks_summary:{legacy_id}",
            f"{runtime.TASK_SUMMARY_CACHE_PREFIX}:{legacy_id}",
        )
        runtime._smart_trace(
            "v2_task_created",
            request_id=request_id,
            conversation_code=conversation_code,
            job_code=job_code,
            character_key=character_key,
            workflow_key=workflow_key,
        )

    except runtime.CharacterPromptValidationError as exc:
        final_error = "character_prompt_validation_failed"
        turn_terminal_status = "failed"
        runtime._add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="error",
            public_message="人物提示词校验失败，本次没有创建任务。请重新描述人物或场景。",
            private_detail=f"v2_character_validation:{type(exc).__name__}",
        )
    except runtime.SmartAgentError as exc:
        final_error = str(getattr(exc, "code", "smart_agent_error"))[:100]
        turn_terminal_status = "failed"
        public = sanitize_public_text(str(exc), fallback="当前请求无法处理，请修改后重试。")
        runtime._add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="error",
            public_message=public,
            private_detail=final_error,
        )
    except Exception as exc:
        final_error = type(exc).__name__[:100]
        turn_terminal_status = "failed"
        detail = str(exc)
        if detail in {
            "余额不足",
            "当前全局队列已满",
            "你当前未完成的任务太多，请等待或取消后再提交",
        }:
            public = runtime._friendly_error(detail)
        elif detail == "workflow_not_found":
            public = "当前没有可用的生成工作流，本次没有创建任务。"
        elif detail == "empty_prompt":
            public = "没有整理出有效的画面方案，请补充描述后重试。"
        else:
            public = "智能 Agent 处理失败，本次没有创建任务。请稍后重试。"
        runtime._smart_trace(
            "v2_turn_failed",
            request_id=request_id,
            conversation_code=conversation_code,
            error_type=type(exc).__name__,
            error_code=detail[:80],
        )
        runtime._add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="error",
            public_message=public,
            private_detail=f"v2:{type(exc).__name__}",
        )
    finally:
        if message_id:
            try:
                status = "failed" if turn_terminal_status == "failed" else "done"
                runtime.mark_smart_agent_message_status(
                    s,
                    message_id=message_id,
                    status=status,
                    error=final_error,
                )
                message_terminal = True
            except Exception:
                pass
            try:
                finish_turn_for_message(
                    s,
                    message_id=message_id,
                    status=turn_terminal_status,
                    error=final_error,
                )
            except Exception:
                pass
