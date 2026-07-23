import asyncio
import hashlib
import io
import json
import mimetypes
import os
import re
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from .agent import refine_prompt
from .auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    ensure_csrf_cookie,
    get_current_user,
    get_current_user_optional,
    get_legacy_user_id_for_session,
    get_session_public_id,
    is_admin_user,
    require_csrf,
    router as auth_router,
)
from .catalog import CONTROL_CHARACTERS, STYLES
from .config import Settings, get_settings
from .recharge_service import (
    ASB_CREDIT_PACKAGES,
    PAYMENT_METHOD_ASB,
    cancel_recharge_request,
    create_recharge_request_for_identity,
    find_pending_topup_for_user,
    get_recharge_request as get_recharge_order,
    list_recharge_requests_for_user,
    mark_recharge_paid_for_user,
    normalize_payment_method,
    parse_rmb_to_fen,
    payment_method_label,
    topup_amount_text,
    topup_credit_text,
    validate_asb_credits,
)
from .redis_client import cache_get_json, cache_set_json, delete as redis_delete, get_redis, incr_with_ttl
from .db import (
    add_conversation_event,
    add_conversation_message,
    cancel_task_atomic,
    claim_next_smart_agent_chat_message,
    claim_next_smart_agent_task,
    clear_conversation,
    confirm_smart_agent_prompt_draft_atomic,
    connect,
    create_conversation,
    create_feedback_report,
    create_fast_translation_task_atomic,
    create_smart_agent_queued_task_atomic,
    create_smart_agent_task_atomic,
    create_task_atomic,
    claim_next_image_refund_review,
    complete_smart_agent_plan,
    create_image_refund_review,
    ensure_schema,
    email_account_has_password,
    fail_fast_translation_task_refund_atomic,
    fail_smart_agent_task_refund,
    add_support_message,
    get_conversation,
    get_conversation_by_code,
    get_conversation_events,
    get_conversation_messages,
    get_latest_task,
    get_latest_relevant_task,
    get_me,
    get_output_owned,
    get_queue_status,
    get_task_by_job_code,
    get_task_queue_position,
    get_user_task_summary,
    get_account_identities,
    get_account_public_profile,
    get_or_create_referral_code,
    get_referral_code,
    get_referral_stats,
    get_image_refund_admin,
    get_image_refund_for_user,
    get_smart_agent_prompt_draft,
    get_pending_topup_submit_reminder,
    get_support_important_unread,
    get_support_thread_by_code,
    get_support_unread_count,
    get_user_settings_row,
    list_support_messages,
    list_support_threads_admin,
    list_support_threads_for_user,
    list_admin_accounts,
    list_conversations,
    list_image_refund_eligible_tasks,
    list_image_refunds_admin,
    list_image_refunds_for_user,
    list_user_tasks,
    list_user_tasks_filtered,
    list_user_tasks_paginated,
    make_job_code,
    mark_smart_agent_message_status,
    mark_referral_campaign_seen,
    has_seen_referral_campaign,
    mark_support_thread_read_by_admin,
    mark_support_thread_read_by_user,
    record_admin_audit,
    reject_manual_image_refund_review,
    request_manual_image_refund_review,
    refund_image_review_atomic,
    reveal_account_email,
    safe_cleanup_input,
    save_image_refund_review_result,
    save_smart_agent_prompt_draft,
    save_user_settings,
    set_image_refund_status,
    set_support_thread_status,
    support_account_display,
    create_support_thread,
    set_account_display_username,
    update_conversation_summary,
    validate_resolution,
    save_pending_disambiguation,
    save_pending_disambiguation_json,
    get_pending_disambiguation,
    get_pending_disambiguation_json,
    resolve_pending_disambiguation,
    supersede_pending_disambiguation,
    clear_pending_disambiguation,
)
from .schemas import (
    AdminImageRefundActionRequest,
    FeedbackCreateRequest,
    ImageRefundCreateRequest,
    ImageRefundManualReviewRequest,
    PromptRefineRequest,
    PromptRefineResponse,
    SmartAgentGenerateRequest,
    SmartAgentMessageRequest,
    SmartAgentTaskRequest,
    SupportMessageCreateRequest,
    SupportThreadCreateRequest,
    TopupCreateRequest,
    ProfileUsernameRequest,
    UserSession,
)
from .services.mimo_review import auto_status_from_result, review_deformed_images
from .smart_agent.planner import SmartAgentClarification, SmartAgentError, build_smart_agent_plan, plan_to_json, _validate_request_policy, serialize_character_ids, parse_character_ids
from .smart_agent.chat_client import chat_with_agent
from .smart_agent.sanitize import sanitize_public_agent_message
from .smart_agent.character_search import find_characters, find_character_after_translation, load_characters, extract_possible_character_names, translate_character_name, build_agent_fallback_character, strip_umamusume_identity_tags, detect_character_disambiguation, resolve_character_from_candidates
from .smart_agent.disambiguation_engine import (
    analyze_character_mentions,
    analyze_user_request,
    create_pending_disambiguation_json,
    validate_character_resolution,
    is_new_generation_request,
    is_disambiguation_choice,
    is_scene_supplement,
    resolve_group,
    all_groups_resolved,
    pending_to_public,
)
from .smart_agent.character_preferences import (
    CharacterPromptValidationError,
    _apply_count_tags,
    _looks_like_identity_tag,
    assemble_character_prompt,
    assemble_character_prompt_with_count,
    character_key as stable_character_key,
    enforce_character_preferences,
    locked_character_tags,
    public_character_matches,
    remove_foreign_character_tags,
    sanitize_inferred_appearance_tags,
    split_prompt_tags,
    validate_character_prompt,
)
from .smart_agent.prompt_library import search_prompt_snippets, snippets_for_prompt
from .smart_agent.workflow_registry import get_workflow, warm_workflow_index, workflow_selection_label, workflow_summaries
from .smart_agent.lora_registry import lora_summaries, sanitize_loras
from .smart_agent.resolution_registry import get_resolution_or_default, ALLOWED_RESOLUTIONS, DEFAULT_RESOLUTION_KEY
from .smart_agent.dynamic_workflows import SMART_AGENT_DEFAULT_WORKFLOW_KEY
from .smart_agent.v2_protocol import prepare_turn, safe_prompt_hidden_reply
from .smart_agent.v2_store import (
    abort_turn,
    begin_turn_atomic,
    bind_turn_message,
    clear_v2_state,
    has_active_turn,
    save_message_resolution,
)
from .smart_agent.v2_worker import process_smart_agent_turn_v2
from .routes.ai_support import router as ai_support_router
from .routes.fast_translate import router as fast_translate_router
from .services.fast_translation_worker import fast_translation_worker_loop

app = FastAPI(title="UMA Web MVP", docs_url=None, redoc_url=None)
settings = get_settings()
TASK_SUMMARY_CACHE_PREFIX = "uma:cache:v2:tasks_summary"
_queue_status_cache = {"expires_at": 0.0, "data": None}
QUEUE_STATUS_CACHE_SECONDS = 3.0
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.app_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
app.include_router(auth_router)
app.include_router(fast_translate_router)
app.include_router(ai_support_router)


def _smart_trace(stage: str, **fields) -> None:
    allowed = {
        "request_id",
        "conversation_code",
        "conversation_id",
        "message_id",
        "job_code",
        "character_key",
        "workflow_key",
        "workflow_source",
        "fallback_level",
        "resolved_intent",
        "should_create_task",
        "nodes",
        "http_status",
        "error_code",
        "error_type",
        "finish_reason",
        "foreign_character_tags_removed_count",
        "selected_count",
        "operation",
        "removed_count",
        # ── Agent 调用审计 ──
        "agent_model",
        "agent_call_count",
        "agent_skip_reason",
        "meaningful_tag_count",
        "generation_readiness",
        "generation_readiness_reason",
        "character_operation",
        "character_source",
        "core_constraint_added_count",
        "core_conflict_removed_count",
        "count",
        "names",
        "translated_name",
        "ambiguous_count",
    }
    clean = []
    for key in sorted(allowed):
        value = fields.get(key)
        if value is None or value == "":
            continue
        clean.append(f"{key}={str(value)[:120]}")
    print(f"[SMART_AGENT_TRACE] stage={stage} {' '.join(clean)}", flush=True)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

CHARACTER_TAGS_PATH = STATIC_DIR / "character-tags.json"

UMAMUSUME_ZH_NAMES = {
    "silence suzuka": "无声铃鹿",
    "tokai teio": "东海帝王",
    "mejiro mcqueen": "目白麦昆",
    "rice shower": "米浴",
    "special week": "特别周",
    "kitasan black": "北部玄驹",
    "satono diamond": "里见光钻",
    "oguri cap": "小栗帽",
    "daiwa scarlet": "大和赤骥",
    "vodka": "伏特加",
    "gold ship": "黄金船",
    "mejiro ryan": "目白赖恩",
    "mejiro bright": "目白光明",
    "mejiro ramonu": "目白拉莫努",
    "mihono bourbon": "美浦波旁",
    "manhattan cafe": "曼城茶座",
    "agnes tachyon": "爱丽速子",
    "nice nature": "优秀素质",
    "twin turbo": "双涡轮",
    "smart falcon": "醒目飞鹰",
    "neo universe": "新宇宙",
    "curren chan": "真机伶",
    "gold city": "黄金城",
    "still in love": "至爱",
    "mayano top gun": "摩耶重炮",
    "verxina": "极峰",
    "copano rickey": "小林历奇",
    "daring tact": "谋勇兼备",
    "daitaku helios": "大拓太阳神",
    "venus paques": "维纳斯帕克斯",
    "super creek": "超级溪流",
    "agnes digital": "爱丽数码",
    "fine motion": "美妙姿势",
    "meisho doto": "名将怒涛",
    "hokko tarumae": "北幸樽前",
    "tamamo cross": "玉藻十字",
    "nishino flower": "西野花",
    "cheval grand": "高尚骏逸",
    "eishin flash": "荣进闪耀",
    "taiki shuttle": "大树快车",
    "narita top road": "成田路",
    "aston machan": "真弓快车",
    "daiichi ruby": "第一红宝石",
    "gentildonna": "贵妇人",
    "vivlos": "高尚骏逸",
    "maruzensky": "丸善斯基",
    "symboli rudolf": "鲁道夫象征",
    "air groove": "气槽",
    "grass wonder": "草上飞",
    "seiun sky": "星云天空",
    "haru urara": "春丽",
}
UMAMUSUME_EN_NAMES = {
    key: " ".join(part.capitalize() for part in key.split())
    for key in UMAMUSUME_ZH_NAMES
}
UMAMUSUME_EN_NAMES.update({
    "tokai teio": "Tokai Teio",
    "mejiro mcqueen": "Mejiro McQueen",
    "daiichi ruby": "Daiichi Ruby",
    "neo universe": "Neo Universe",
    "manhattan cafe": "Manhattan Café",
    "gold city": "Gold City",
    "still in love": "Still In Love",
    "mayano top gun": "Mayano Top Gun",
    "copano rickey": "Copano Rickey",
    "daring tact": "Daring Tact",
    "daitaku helios": "Daitaku Helios",
    "venus paques": "Venus Paques",
    "super creek": "Super Creek",
    "agnes digital": "Agnes Digital",
    "fine motion": "Fine Motion",
    "meisho doto": "Meisho Doto",
    "hokko tarumae": "Hokko Tarumae",
    "tamamo cross": "Tamamo Cross",
    "nishino flower": "Nishino Flower",
    "cheval grand": "Cheval Grand",
    "eishin flash": "Eishin Flash",
    "taiki shuttle": "Taiki Shuttle",
    "narita top road": "Narita Top Road",
    "aston machan": "Aston Machan",
})
FEEDBACK_CATEGORIES = {
    "add_character",
    "bug_report",
    "feature_request",
    "payment_issue",
    "other",
}


class RateLimiter:
    def __init__(self):
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: int, *, limit_type: str = "rate_limit") -> None:
        now = time.monotonic()
        queue = self.events[key]
        while queue and queue[0] <= now - window_seconds:
            queue.popleft()
        if len(queue) >= limit:
            retry_after = max(1, int((queue[0] + window_seconds) - now) + 1)
            raise HTTPException(
                status_code=429,
                detail={
                    "detail": "请求过于频繁,请稍后再试",
                    "retry_after": retry_after,
                    "limit_type": limit_type,
                },
            )
        queue.append(now)


limiter = RateLimiter()

OUTPUT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; "
        "connect-src 'self' https://cloudflareinsights.com; frame-ancestors 'none'; base-uri 'self'"
    )
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith(("/api/image-refunds", "/api/admin/image-refunds")):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


async def smart_agent_worker_loop() -> None:
    while True:
        try:
            if not settings.smart_agent_enabled or not settings.deepseek_api_key:
                await asyncio.sleep(5)
                continue
            task = await asyncio.to_thread(claim_next_smart_agent_task, settings)
            if not task:
                await asyncio.sleep(2)
                continue
            job_code = str(task["job_code"])
            request_text = str(task.get("smart_agent_request") or task.get("original_prompt") or "")
            prompt_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:12]
            print(f"[SMART_AGENT] planning job={job_code} prompt_hash={prompt_hash} prompt_len={len(request_text)}", flush=True)
            try:
                plan = await build_smart_agent_plan(
                    settings,
                    request_text,
                    is_admin=str(task.get("user_id")) == settings.owner_user_id,
                    task_prompt_source=str(task.get("prompt_source") or ""),
                    task_character_key=str(task.get("character_key") or ""),
                )
                ok = await asyncio.to_thread(
                    complete_smart_agent_plan,
                    settings,
                    job_code=job_code,
                    plan_json=plan_to_json(plan),
                    prompt=plan["positive_prompt"],
                    workflow_key=plan["workflow_key"],
                    loras_json=json.dumps(plan.get("loras") or [], ensure_ascii=False, separators=(",", ":")),
                    prompt_source=plan.get("prompt_source") or "deepseek",
                    width=int(plan["width"]),
                    height=int(plan["height"]),
                    character_key=str(plan.get("character_key") or ""),
                    workflow_source=str(plan.get("fallback_level") or ""),
                    fallback_level=str(plan.get("fallback_level") or ""),
                )
                if ok:
                    redis_delete(
                        settings,
                        "uma:cache:queue_status",
                        f"uma:cache:tasks_summary:{task.get('user_id')}",
                        f"{TASK_SUMMARY_CACHE_PREFIX}:{task.get('user_id')}",
                    )
                    print(f"[SMART_AGENT] queued job={job_code} workflow={plan['workflow_key']}", flush=True)
                else:
                    print(f"[SMART_AGENT] skipped job={job_code} state_changed", flush=True)
            except SmartAgentClarification as exc:
                await asyncio.to_thread(
                    fail_smart_agent_task_refund,
                    settings,
                    job_code=job_code,
                    error=str(exc),
                    error_code=exc.code,
                )
                print(f"[SMART_AGENT] clarification_refund job={job_code}", flush=True)
            except SmartAgentError as exc:
                await asyncio.to_thread(
                    fail_smart_agent_task_refund,
                    settings,
                    job_code=job_code,
                    error=str(exc),
                    error_code=exc.code,
                )
                print(f"[SMART_AGENT] failed_refund job={job_code} code={exc.code}", flush=True)
            except Exception as exc:
                await asyncio.to_thread(
                    fail_smart_agent_task_refund,
                    settings,
                    job_code=job_code,
                    error="Smart Agent planning failed",
                    error_code=type(exc).__name__[:80],
                )
                print(f"[SMART_AGENT] failed_refund job={job_code} code={type(exc).__name__}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SMART_AGENT] worker_error code={type(exc).__name__}", flush=True)
            await asyncio.sleep(5)


def _image_review_paths(review: dict[str, Any]) -> list[Path]:
    try:
        output_ids = [int(item) for item in json.loads(review.get("output_ids_json") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        output_ids = []
    if not output_ids:
        return []
    placeholders = ",".join("?" for _ in output_ids)
    conn = connect(settings)
    try:
        rows = conn.execute(
            f"SELECT id,file_path FROM generation_outputs WHERE id IN ({placeholders}) ORDER BY id",
            output_ids,
        ).fetchall()
    finally:
        conn.close()
    paths: list[Path] = []
    for row in rows:
        try:
            paths.append(settings.resolve_output_path(row["file_path"]))
        except Exception:
            continue
    return paths


def _serve_output_row(row: dict[str, Any], s: Settings, *, download_name: str | None = None) -> FileResponse:
    try:
        path = s.resolve_output_path(row["file_path"])
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="图片文件不存在")
    allowed_roots = [s.bot_output_dir.resolve()]
    if s.is_local_env() and s.mock_worker_enabled:
        allowed_roots.append(s.mock_output_path.resolve())
    if not any(root in path.parents or path == root for root in allowed_roots) or not path.is_file() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="图片文件不存在")
    media_type = OUTPUT_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        raise HTTPException(status_code=404, detail="图片文件不存在")
    return FileResponse(path, media_type=media_type, filename=download_name or path.name)


async def image_refund_reviewer_loop() -> None:
    while True:
        try:
            review = await asyncio.to_thread(claim_next_image_refund_review, settings)
            if not review:
                await asyncio.sleep(3)
                continue
            review_code = str(review["review_code"])
            try:
                image_paths = _image_review_paths(review)
                if not image_paths:
                    result = {
                        "decision": "manual_review",
                        "all_outputs_severely_deformed": False,
                        "severity_score": 0,
                        "confidence": 0.0,
                        "reason_codes": ["missing_outputs"],
                        "minor_only": False,
                        "six_fingers_only": False,
                        "usable_output_exists": False,
                        "public_reason_zh": "输出图片无法读取,已进入人工复核。",
                    }
                else:
                    result = await review_deformed_images(
                        settings,
                        original_request=str(review.get("original_request_snapshot") or ""),
                        final_prompt=str(review.get("final_prompt_snapshot") or ""),
                        user_note=str(review.get("user_note") or ""),
                        image_paths=image_paths,
                    )
                status = auto_status_from_result(settings, result)
                await asyncio.to_thread(save_image_refund_review_result, settings, review_code, result, status=status)
                if status == "approved":
                    await asyncio.to_thread(set_image_refund_status, settings, review_code, "refund_pending")
                    await asyncio.to_thread(refund_image_review_atomic, settings, review_code, "mimo_auto_review")
                print(
                    f"[IMAGE_REFUND] reviewed code={review_code} status={status} "
                    f"severity={int(result.get('severity_score') or 0)} confidence={float(result.get('confidence') or 0):.2f}",
                    flush=True,
                )
            except Exception as exc:
                result = {
                    "decision": "manual_review",
                    "all_outputs_severely_deformed": False,
                    "severity_score": 0,
                    "confidence": 0.0,
                    "reason_codes": [type(exc).__name__[:80]],
                    "minor_only": False,
                    "six_fingers_only": False,
                    "usable_output_exists": False,
                    "public_reason_zh": "自动审核暂时无法完成,已进入人工复核。",
                }
                await asyncio.to_thread(save_image_refund_review_result, settings, review_code, result, status="manual_review")
                print(f"[IMAGE_REFUND] review_error code={review_code} error={type(exc).__name__}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[IMAGE_REFUND] worker_error code={type(exc).__name__}", flush=True)
            await asyncio.sleep(5)


@app.on_event("startup")
async def startup() -> None:
    settings.validate_runtime()
    settings.validate_local_isolation()
    ensure_schema(settings)
    settings.input_image_dir.mkdir(parents=True, exist_ok=True)
    settings.bot_output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    finally:
        conn.close()
    print(
        f"[RUNTIME] web startup ok sqlite_journal_mode={journal_mode} "
        f"sqlite_busy_timeout={busy_timeout} sqlite_synchronous={synchronous}",
        flush=True,
    )
    dynamic_workflow_count = warm_workflow_index()
    print(f"[RUNTIME] smart_agent_dynamic_workflows indexed={dynamic_workflow_count}", flush=True)
    default_registered = bool(get_workflow(SMART_AGENT_DEFAULT_WORKFLOW_KEY))
    print(
        f"[RUNTIME] smart_agent_default workflow registered: key={SMART_AGENT_DEFAULT_WORKFLOW_KEY} "
        f"ok={str(default_registered).lower()}",
        flush=True,
    )
    redis_state = "disabled"
    if settings.redis_enabled:
        redis_state = "connected" if get_redis(settings) is not None else "fallback"
    print(f"[RUNTIME] redis {redis_state}", flush=True)
    print(
        f"[RUNTIME] MiMo enabled: {'yes' if settings.mimo_image_review_enabled else 'no'}",
        flush=True,
    )
    print(
        f"[RUNTIME] MiMo API key configured: {'yes' if bool(settings.mimo_image_review_api_key) else 'no'}",
        flush=True,
    )
    print(f"[RUNTIME] MiMo model: {settings.mimo_image_review_model}", flush=True)
    app.state.smart_agent_worker = asyncio.create_task(smart_agent_worker_loop())
    app.state.smart_agent_chat_worker = asyncio.create_task(smart_agent_chat_worker_loop())
    app.state.image_refund_worker = asyncio.create_task(image_refund_reviewer_loop())
    app.state.fast_translation_worker = asyncio.create_task(fast_translation_worker_loop(settings))


@app.on_event("shutdown")
async def shutdown() -> None:
    worker = getattr(app.state, "smart_agent_worker", None)
    if worker:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    chat_worker = getattr(app.state, "smart_agent_chat_worker", None)
    if chat_worker:
        chat_worker.cancel()
        try:
            await chat_worker
        except asyncio.CancelledError:
            pass
    refund_worker = getattr(app.state, "image_refund_worker", None)
    if refund_worker:
        refund_worker.cancel()
        try:
            await refund_worker
        except asyncio.CancelledError:
            pass
    ft_worker = getattr(app.state, "fast_translation_worker", None)
    if ft_worker:
        ft_worker.cancel()
        try:
            await ft_worker
        except asyncio.CancelledError:
            pass


@app.get("/")
def index(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return FileResponse(STATIC_DIR / "login.html")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(
    user: UserSession | None = Depends(get_current_user_optional),
    settings: Settings = Depends(get_settings),
):
    # 已登录用户访问 /login → 直接跳转到首页，避免前端重定向循环
    if user is not None:
        return RedirectResponse(url="/", status_code=302)
    response = FileResponse(STATIC_DIR / "login.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "branding" / "favicon.ico", media_type="image/x-icon")


@app.get("/settings")
def settings_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "settings.html")



@app.get("/topup")
def topup_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "topup.html")


@app.get("/profile")
def profile_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "profile.html")


@app.get("/messages")
def messages_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "messages.html")


@app.get("/image-refund")
def image_refund_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "image-refund.html")


@app.get("/admin/support")
def admin_support_page(
    user: UserSession | None = Depends(get_current_user_optional),
    s: Settings = Depends(get_settings),
):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    _require_admin(user, s)
    return FileResponse(STATIC_DIR / "admin-support.html")


@app.get("/admin/image-refunds")
def admin_image_refunds_page(
    user: UserSession | None = Depends(get_current_user_optional),
    s: Settings = Depends(get_settings),
):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    _require_admin(user, s)
    return FileResponse(STATIC_DIR / "admin-image-refunds.html")


@app.get("/smart-agent")
def smart_agent_page(user: UserSession | None = Depends(get_current_user_optional), s: Settings = Depends(get_settings)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if s.ai_support_enabled:
        return FileResponse(STATIC_DIR / "ai-support.html")
    if s.smart_agent_enabled:
        return FileResponse(STATIC_DIR / "smart-agent.html")
    raise HTTPException(status_code=404, detail="功能未开放")


@app.get("/smart-agent-legacy")
def smart_agent_legacy_page(
    user: UserSession | None = Depends(get_current_user_optional),
    s: Settings = Depends(get_settings),
):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if not (s.is_local_env() and s.smart_agent_legacy_enabled and s.dev_auth_bypass):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(STATIC_DIR / "smart-agent.html")


@app.get("/smart-agent-preview")
def smart_agent_preview_page():
    return FileResponse(
        STATIC_DIR / "smart-agent-preview.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/smart-agent-preview.css")
def smart_agent_preview_css():
    return Response(
        (STATIC_DIR / "smart-agent-preview.css").read_text(encoding="utf-8"),
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/smart-agent-preview.js")
def smart_agent_preview_js():
    return Response(
        (STATIC_DIR / "smart-agent-preview.js").read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/character-tags")
def character_tags_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "character-tags.html")

@app.get("/feedback")
def feedback_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "feedback.html")

@app.get("/change-password")
def change_password_page(user: UserSession | None = Depends(get_current_user_optional)):
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return FileResponse(STATIC_DIR / "change-password.html")

# Hash redirects for backward compatibility
@app.get("/settings/character-tags")
def settings_character_tags_redirect():
    return RedirectResponse(url="/character-tags", status_code=301)

@app.get("/settings/feedback")
def settings_feedback_redirect():
    return RedirectResponse(url="/feedback", status_code=301)

@app.get("/settings/change-password")
def settings_change_password_redirect():
    return RedirectResponse(url="/change-password", status_code=301)

@app.get("/api/health")
def health():
    return {"ok": True}


def _desktop_umamusume_path() -> Path:
    return Path(os.path.expanduser("~")) / "Desktop" / "umamusume.txt"


def _canonical_name_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(
        ascii_name.lower()
        .replace("_", " ")
        .replace("-", " ")
        .replace("(", " ")
        .replace(")", " ")
        .split()
    )


def _ensure_umamusume_tags(tags: str) -> str:
    parts = [part.strip() for part in tags.split(",") if part.strip()]
    lowered = {part.lower() for part in parts}
    for required in ("umamusume", "horse ears", "horse tail"):
        if required not in lowered:
            parts.append(required)
    return ", ".join(parts)


def _normalize_tag_item(raw: str) -> dict:
    text = " ".join(raw.strip().split())
    if "," in text:
        name_part, tag_part = text.split(",", 1)
        name_en = name_part.strip()
        tag_name = tag_part.split(",", 1)[0].strip() or name_en
        tag_key = _canonical_name_key(tag_name)
        tags = _ensure_umamusume_tags(f"umamusume, {tag_key}, {tag_key} (umamusume)")
    else:
        name_en = text
        lowered = _canonical_name_key(name_en)
        tags = f"umamusume, {lowered}, {lowered} (umamusume), horse ears, horse tail"
    key = _canonical_name_key(name_en)
    name_en = UMAMUSUME_EN_NAMES.get(key, name_en)
    return {
        "name_zh": UMAMUSUME_ZH_NAMES.get(key, name_en),
        "name_en": name_en,
        "tags": tags,
    }


def _load_umamusume_tags() -> tuple[dict, bool]:
    path = _desktop_umamusume_path()
    if not path.is_file():
        return {
            "category_zh": "赛马娘",
            "category_en": "Umamusume",
            "items": [],
            "source": "desktop_missing",
        }, False
    items: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            items.append(_normalize_tag_item(text))
    except UnicodeDecodeError:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            items.append(_normalize_tag_item(text))
    return {
        "category_zh": "赛马娘",
        "category_en": "Umamusume",
        "items": items,
        "source": "desktop",
    }, True


@app.get("/api/character-tags")
def character_tags(user: UserSession = Depends(get_current_user)):
    grouped: dict[str, dict[str, Any]] = {}
    category_order = 0
    for character in load_characters():
        category_en = str(character.get("category_en") or "Other Anime")
        category_zh = str(character.get("category_zh") or "其他动漫")
        category_key = _category_key(category_en, category_zh)
        bucket = grouped.get(category_key)
        if bucket is None:
            bucket = {
                "category_key": category_key,
                "category_zh": "赛马娘" if category_key == "umamusume" else category_zh,
                "category_en": "Umamusume" if category_key == "umamusume" else category_en,
                "_order": category_order,
                "items": [],
            }
            grouped[category_key] = bucket
            category_order += 1
        bucket["items"].append(
            {
                "character_key": str(character.get("key") or ""),
                "name_zh": str(character.get("name_zh") or ""),
                "name_en": str(character.get("name_en") or ""),
                "aliases": str(character.get("aliases") or ""),
                "tags": str(character.get("tags") or ""),
            }
        )
    categories = sorted(grouped.values(), key=_category_sort_key)
    for category in categories:
        category.pop("_order", None)
    return {
        "categories": categories,
        "umamusume_desktop_found": _desktop_umamusume_path().is_file(),
    }


def _category_key(category_en: str, category_zh: str) -> str:
    normalized = _canonical_name_key(category_en or category_zh)
    if normalized == "umamusume" or category_zh == "赛马娘":
        return "umamusume"
    if normalized in {"anime", "other anime"} or category_zh == "其他动漫":
        return "other_anime"
    return normalized.replace(" ", "_") or "other_anime"


def _category_sort_key(category: dict[str, Any]) -> tuple[int, str]:
    key = str(category.get("category_key") or "")
    if key == "other_anime":
        return (9999, key)
    return (int(category.get("_order") or 0), key)


@app.post("/api/feedback")
def create_feedback(
    body: FeedbackCreateRequest,
    user: UserSession = Depends(get_current_user),
    csrf: None = Depends(require_csrf),
    s: Settings = Depends(get_settings),
):
    category = (body.category or "other").strip()
    if category not in FEEDBACK_CATEGORIES:
        raise HTTPException(status_code=400, detail="反馈类型无效")
    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="反馈内容不能为空")
    if len(message) > 1000:
        raise HTTPException(status_code=400, detail="反馈内容不能超过 1000 字")
    legacy_id = get_legacy_user_id_for_session(user, s)
    user_display = user.username
    conn = connect(s)
    try:
        profile = get_account_public_profile(conn, user.user_id)
        if profile:
            user_display = profile.get("display_label") or user_display
    finally:
        conn.close()
    feedback_id = create_feedback_report(
        s,
        account_id=user.user_id,
        legacy_user_id=str(legacy_id),
        provider=user.provider,
        user_display=user_display,
        category=category,
        message=message,
    )
    return {"ok": True}


@app.post("/api/feedback/")
def create_feedback_slash(
    body: FeedbackCreateRequest,
    user: UserSession = Depends(get_current_user),
    csrf: None = Depends(require_csrf),
    s: Settings = Depends(get_settings),
):
    return create_feedback(body=body, user=user, csrf=csrf, s=s)


@app.get("/api/me")
def me(
    response: Response,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
):
    ensure_csrf_cookie(response, s, session, csrf_cookie)
    legacy_id = get_legacy_user_id_for_session(user, s)
    data = get_me(s, legacy_id)

    # Get identity bindings
    identities = []
    has_email_password = False
    welcome_bonus_granted = False
    if not s.dev_auth_bypass:
        conn = connect(s)
        try:
            identities = get_account_identities(conn, user.user_id)
            if user.provider == "email":
                has_email_password = email_account_has_password(conn, user.user_id)
            account_row = conn.execute(
                "SELECT created_at, welcome_credits_granted_at, display_username FROM accounts WHERE id=?",
                (user.user_id,),
            ).fetchone()
            if account_row and account_row["welcome_credits_granted_at"]:
                welcome_at = int(account_row["welcome_credits_granted_at"])
                created_at = int(account_row["created_at"])
                welcome_bonus_granted = welcome_at <= created_at + 60
            if account_row and account_row["display_username"]:
                user.username = str(account_row["display_username"])
        finally:
            conn.close()

    bound_providers = {i["provider"] for i in identities}
    return {
        "user_id": user.user_id,
        "account_id": user.user_id,
        "legacy_user_id": legacy_id,
        "discord_user_id": user.discord_user_id,
        "username": user.username,
        "display_username": user.username,
        "avatar": user.avatar,
        "provider": user.provider,
        "balance_fen": data["balance_fen"],
        "settings": data["settings"],
        "price_fen_per_image": s.price_fen_per_image,
        "agent_enabled": s.agent_enabled,
        "agent_surcharge_credits": int(s.agent_surcharge_credits),
        "normal_translator_cost_credits": int(s.agent_surcharge_credits),
        "app_env": s.app_env,
        "fast_translator_enabled": bool(s.fast_translator_enabled),
        "fast_translator_cost_credits": int(s.fast_translator_cost_credits),
        "ai_support_enabled": bool(s.ai_support_enabled),
        "smart_agent_enabled": bool(s.smart_agent_enabled),
        "is_admin": is_admin_user(user, s),
        "email_auth_available": s.is_email_auth_available(),
        "has_email_password": has_email_password,
        "bound_providers": list(bound_providers),
        "identities": identities,
        "dev_auth_bypass": s.dev_auth_bypass,
        "welcome_bonus_granted": welcome_bonus_granted,
        "session_public_id": get_session_public_id(s, session),
    }


@app.get("/api/profile")
def profile_api(
    response: Response,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    conn = connect(s)
    try:
        profile = get_account_public_profile(conn, user.user_id)
    finally:
        conn.close()
    if not profile:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "account_id": profile["account_id"],
        "provider": profile["provider"],
        "display_name": profile["display_name"],
        "display_username": profile.get("display_username") or "",
        "display_label": profile.get("display_label") or user.username,
        "email_masked": profile.get("email_masked"),
        "legacy_user_id": profile.get("legacy_user_id"),
        "balance_fen": int(profile.get("balance_fen") or 0),
        "created_at": profile.get("created_at"),
        "last_login_at": profile.get("last_login_at"),
        "referral_code": profile.get("referral_code") or "",
        "referral_code_created_at": profile.get("referral_code_created_at"),
        "referral_campaign_enabled": bool(s.referral_campaign_enabled),
        "referral_invitee_bonus_credits": int(s.referral_invitee_bonus_credits),
        "referral_inviter_bonus_credits": int(s.referral_inviter_bonus_credits),
        "referral_stats": {
            "invited_count": int(profile.get("invited_count") or 0),
            "inviter_reward_credits": int(profile.get("inviter_reward_credits") or 0),
        },
    }


@app.post("/api/profile/username")
def update_profile_username(
    body: ProfileUsernameRequest,
    response: Response,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = set_account_display_username(conn, user.user_id, body.display_username)
        conn.commit()
        print(f"[PROFILE] display_username_updated account_id={user.user_id}", flush=True)
        return {"ok": True, **result}
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError:
        conn.rollback()
        raise HTTPException(status_code=404, detail="Not found")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/profile/referral-code")
def claim_profile_referral_code(
    response: Response,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    if not s.referral_campaign_enabled:
        raise HTTPException(status_code=403, detail="邀请活动暂未开放")
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = get_or_create_referral_code(conn, user.user_id)
        conn.commit()
        return {"ok": True, **result}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/profile/referral-stats")
def profile_referral_stats(
    response: Response,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    conn = connect(s)
    try:
        code = get_referral_code(conn, user.user_id)
        stats = get_referral_stats(conn, user.user_id)
        return {"referral_code": code["referral_code"] if code else "", **stats}
    finally:
        conn.close()


@app.get("/api/referral-campaign")
def referral_campaign_status(
    response: Response,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    enabled = bool(s.referral_campaign_enabled)
    seen = True
    conn = connect(s)
    try:
        if enabled:
            seen = has_seen_referral_campaign(conn, user.user_id, s.referral_campaign_version)
    finally:
        conn.close()
    return {
        "enabled": enabled,
        "version": s.referral_campaign_version,
        "show": enabled and not seen,
        "invitee_bonus_credits": int(s.referral_invitee_bonus_credits),
        "inviter_bonus_credits": int(s.referral_inviter_bonus_credits),
    }


@app.post("/api/referral-campaign/seen")
def mark_referral_campaign_seen_api(
    response: Response,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    response.headers["Cache-Control"] = "no-store"
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        mark_referral_campaign_seen(conn, user.user_id, s.referral_campaign_version)
        conn.commit()
        return {"ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _require_admin(user: UserSession, settings: Settings) -> None:
    if not is_admin_user(user, settings):
        raise HTTPException(status_code=404, detail="Not found")


def _serialize_support_thread(thread: dict[str, Any], *, admin: bool = False) -> dict[str, Any]:
    item = {
        "thread_code": thread["thread_code"],
        "account_id": thread["account_id"] if admin else None,
        "legacy_user_id": thread.get("legacy_user_id") if admin else None,
        "category": thread["category"],
        "subject": thread.get("subject") or "",
        "related_feedback_id": thread.get("related_feedback_id"),
        "related_topup_code": thread.get("related_topup_code"),
        "status": thread["status"],
        "priority": thread["priority"],
        "created_at": int(thread["created_at"] or 0),
        "updated_at": int(thread["updated_at"] or 0),
        "closed_at": thread.get("closed_at"),
        "unread_user_count": int(thread.get("unread_user_count") or 0),
        "unread_admin_count": int(thread.get("unread_admin_count") or 0),
        "last_message_at": thread.get("last_message_at"),
    }
    if admin:
        item["provider"] = thread.get("provider")
        item["display_name"] = support_account_display(thread)
        item["email_masked"] = thread.get("email_masked")
        item["balance_fen"] = int(thread.get("balance_fen") or 0)
    return item


def _serialize_support_message(message: dict[str, Any], *, admin: bool = False) -> dict[str, Any]:
    item = {
        "id": int(message["id"]),
        "sender_type": message["sender_type"],
        "body": message["body"],
        "created_at": int(message["created_at"] or 0),
        "read_by_user_at": message.get("read_by_user_at"),
        "read_by_admin_at": message.get("read_by_admin_at"),
    }
    if admin:
        item["sender_account_id"] = message.get("sender_account_id")
    return item


def _support_thread_or_404(
    conn,
    thread_code: str,
    *,
    account_id: str | None = None,
    include_counts: bool = True,
) -> dict[str, Any]:
    thread = get_support_thread_by_code(conn, thread_code, account_id=account_id, include_counts=include_counts)
    if not thread:
        raise HTTPException(status_code=404, detail="Not found")
    return thread


@app.get("/api/support/threads")
def support_threads(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    conn = connect(s)
    try:
        rows = list_support_threads_for_user(conn, user.user_id)
        return {"items": [_serialize_support_thread(row) for row in rows]}
    finally:
        conn.close()


@app.get("/api/support/threads/{thread_code}")
def support_thread_detail(
    thread_code: str,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    conn = connect(s)
    try:
        thread = _support_thread_or_404(conn, thread_code, account_id=user.user_id)
        messages = list_support_messages(conn, int(thread["id"]))
        return {
            "thread": _serialize_support_thread(thread),
            "messages": [_serialize_support_message(msg) for msg in messages],
        }
    finally:
        conn.close()


@app.post("/api/support/threads/{thread_code}/messages")
def support_thread_reply(
    thread_code: str,
    body: SupportMessageCreateRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    limiter.check(f"support:user:{user.user_id}", 10, 60)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = _support_thread_or_404(conn, thread_code, account_id=user.user_id)
        if thread["status"] != "open":
            conn.rollback()
            raise HTTPException(status_code=400, detail="该会话已结束,不能继续回复。")
        message = add_support_message(
            conn,
            thread_id=int(thread["id"]),
            sender_type="user",
            sender_account_id=user.user_id,
            body=body.message,
        )
        conn.commit()
        return {"ok": True, "message": _serialize_support_message(message)}
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/support/threads/{thread_code}/read")
def support_thread_read(
    thread_code: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = _support_thread_or_404(conn, thread_code, account_id=user.user_id)
        changed = mark_support_thread_read_by_user(conn, int(thread["id"]))
        conn.commit()
        return {"ok": True, "read_count": changed}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/support/unread-count")
def support_unread_count(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    conn = connect(s)
    try:
        important = get_support_important_unread(conn, user.user_id)
        payload = {"unread_count": get_support_unread_count(conn, user.user_id)}
        if important:
            payload["important"] = {
                "thread_code": important["thread_code"],
                "message_id": int(important["message_id"]),
                "priority": important["priority"],
                "subject": important.get("subject") or "管理员消息",
                "body_preview": str(important.get("body") or "")[:180],
            }
        return payload
    finally:
        conn.close()


@app.get("/api/topup/pending-submit-reminder")
def pending_topup_submit_reminder(
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    conn = connect(s)
    try:
        reminder = get_pending_topup_submit_reminder(conn, legacy_user_id=legacy_id)
    finally:
        conn.close()
    if not reminder:
        return {"show": False}
    return {
        "show": True,
        "topup_code": reminder["code"],
        "created_at": int(reminder["created_at"]),
        "reminder_id": reminder["reminder_id"],
        "count": int(reminder.get("count") or 1),
    }


@app.get("/api/admin/support/threads")
def admin_support_threads(
    account_id: str | None = None,
    status: str | None = None,
    category: str | None = None,
    unread_only: bool = False,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        rows = list_support_threads_admin(
            conn,
            account_id=account_id,
            status=status,
            category=category,
            unread_only=unread_only,
        )
        return {"items": [_serialize_support_thread(row, admin=True) for row in rows]}
    finally:
        conn.close()


@app.post("/api/admin/support/threads")
def admin_support_thread_create(
    body: SupportThreadCreateRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = create_support_thread(
            conn,
            account_id=body.account_id,
            admin_account_id=user.user_id,
            category=body.category,
            subject=body.subject,
            message=body.message,
            related_feedback_id=body.related_feedback_id,
            related_topup_code=body.related_topup_code,
            priority=body.priority,
        )
        record_admin_audit(conn, user.user_id, body.account_id, "support_thread_create")
        conn.commit()
        return {"ok": True, "thread": _serialize_support_thread(thread, admin=True)}
    except LookupError:
        conn.rollback()
        raise HTTPException(status_code=404, detail="Not found")
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/admin/support/threads/{thread_code}")
def admin_support_thread_detail(
    thread_code: str,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = _support_thread_or_404(conn, thread_code)
        changed = mark_support_thread_read_by_admin(conn, int(thread["id"]))
        if changed:
            conn.commit()
            thread = _support_thread_or_404(conn, thread_code)
        else:
            conn.commit()
        messages = list_support_messages(conn, int(thread["id"]))
        return {
            "thread": _serialize_support_thread(thread, admin=True),
            "messages": [_serialize_support_message(msg, admin=True) for msg in messages],
        }
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/admin/support/threads/{thread_code}/messages")
def admin_support_thread_message(
    thread_code: str,
    body: SupportMessageCreateRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = _support_thread_or_404(conn, thread_code)
        if thread["status"] != "open":
            conn.rollback()
            raise HTTPException(status_code=400, detail="该会话已结束,请先重新打开。")
        message = add_support_message(
            conn,
            thread_id=int(thread["id"]),
            sender_type="admin",
            sender_admin_id=user.user_id,
            body=body.message,
        )
        record_admin_audit(conn, user.user_id, thread["account_id"], "support_message_send")
        conn.commit()
        return {"ok": True, "message": _serialize_support_message(message, admin=True)}
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_admin_support_status(thread_code: str, status: str, user: UserSession, s: Settings) -> dict[str, Any]:
    _require_admin(user, s)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = _support_thread_or_404(conn, thread_code)
        set_support_thread_status(conn, int(thread["id"]), status)
        record_admin_audit(conn, user.user_id, thread["account_id"], f"support_thread_{status}")
        conn.commit()
        thread = _support_thread_or_404(conn, thread_code)
        return {"ok": True, "thread": _serialize_support_thread(thread, admin=True)}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/admin/support/threads/{thread_code}/close")
def admin_support_thread_close(
    thread_code: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _set_admin_support_status(thread_code, "closed", user, s)


@app.post("/api/admin/support/threads/{thread_code}/reopen")
def admin_support_thread_reopen(
    thread_code: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _set_admin_support_status(thread_code, "open", user, s)


@app.get("/api/admin/accounts")
def admin_accounts(
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        rows = list_admin_accounts(conn, limit=limit, offset=offset, query=query, settings=s)
    finally:
        conn.close()
    items = []
    for row in rows:
        provider = row["provider"]
        display = row["email_masked"] if provider == "email" else row["display_name"]
        display_username = row.get("display_username") or ""
        items.append({
            "account_id": row["account_id"],
            "provider": provider,
            "display_name": display_username or display,
            "base_display_name": display,
            "display_username": display_username,
            "email_masked": row["email_masked"],
            "legacy_user_id": row["legacy_user_id"],
            "balance_fen": int(row["balance_fen"] or 0),
            "referral_code": row.get("referral_code") or "",
            "referral_count": int(row.get("referral_count") or 0),
            "referral_reward_credits": int(row.get("referral_reward_credits") or 0),
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"],
            "task_count": int(row["task_count"] or 0),
            "topup_count": int(row["topup_count"] or 0),
        })
    return {"items": items}


@app.post("/api/admin/accounts/{account_id}/email/reveal")
def admin_account_email(
    account_id: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        email = reveal_account_email(conn, s, account_id)
        if not email:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Not found")
        record_admin_audit(conn, user.user_id, account_id, "email_reveal")
        conn.commit()
        return {"account_id": account_id, "email": email}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _current_account_id(user: UserSession) -> str:
    return str(user.user_id)


def _public_review(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_code": item.get("review_code"),
        "job_code": item.get("job_code"),
        "status": item.get("status"),
        "charged_credits": int(item.get("charged_credits") or 0),
        "decision": item.get("decision") or "",
        "severity_score": item.get("severity_score"),
        "confidence": item.get("confidence"),
        "public_reason": item.get("public_reason") or "",
        "created_at": item.get("created_at"),
        "reviewed_at": item.get("reviewed_at"),
        "refunded_at": item.get("refunded_at"),
        "manual_review_requested_at": item.get("manual_review_requested_at"),
        "manual_review_decided_at": item.get("manual_review_decided_at"),
        "manual_review_decision": item.get("manual_review_decision") or "",
        "manual_review_reason": item.get("manual_review_reason") or "",
        "manual_review_attempts": int(item.get("manual_review_attempts") or 0),
        "can_request_manual_review": bool(item.get("can_request_manual_review")),
        "outputs": item.get("outputs") or [],
    }


@app.get("/api/image-refunds/eligible-tasks")
def image_refund_eligible_tasks(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    return {"ok": True, "items": list_image_refund_eligible_tasks(s, legacy_id, _current_account_id(user))}


@app.get("/api/image-refunds")
def image_refund_reviews(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    return {"ok": True, "items": [_public_review(item) for item in list_image_refunds_for_user(s, _current_account_id(user))]}


@app.get("/api/image-refunds/{review_code}")
def image_refund_detail(review_code: str, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    item = get_image_refund_for_user(s, _current_account_id(user), review_code.upper())
    if not item:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    return {"ok": True, "item": _public_review(item)}


@app.post("/api/image-refunds")
def image_refund_create(
    body: ImageRefundCreateRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if not body.confirm_severe_only:
        raise HTTPException(status_code=400, detail="请先确认该问题属于严重结构崩坏")
    legacy_id = get_legacy_user_id_for_session(user, s)
    try:
        result = create_image_refund_review(
            s,
            account_id=_current_account_id(user),
            legacy_user_id=legacy_id,
            job_code=body.job_code,
            user_note=body.user_note or "",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "created": bool(result.get("created")), "item": _public_review(result["review"])}


@app.post("/api/image-refunds/{review_code}/request-manual-review")
def image_refund_request_manual_review(
    review_code: str,
    body: ImageRefundManualReviewRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    try:
        item = request_manual_image_refund_review(
            s,
            account_id=_current_account_id(user),
            review_code=review_code,
            user_note=body.user_note or "",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="审核记录不存在") from exc
    except RuntimeError as exc:
        detail = str(exc)
        status_code = 409 if detail in {"manual_review_final_rejected", "manual_review_not_available"} else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {"ok": True, "item": _public_review(item), "already_requested": bool(item.get("already_requested"))}


@app.get("/api/admin/image-refunds")
def admin_image_refund_list(
    status: str = "",
    limit: int = 100,
    offset: int = 0,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    return {"ok": True, "items": list_image_refunds_admin(s, status=status, limit=limit, offset=offset)}


@app.get("/api/admin/image-refunds/{review_code}")
def admin_image_refund_detail(review_code: str, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    _require_admin(user, s)
    item = get_image_refund_admin(s, review_code.upper())
    if not item:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    return {"ok": True, "item": item}


@app.get("/api/admin/image-refunds/{review_code}/outputs/{output_id}")
def admin_image_refund_output(
    review_code: str,
    output_id: int,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    _require_admin(user, s)
    item = get_image_refund_admin(s, review_code.upper())
    if not item:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    try:
        output_ids = [int(value) for value in json.loads(item.get("output_ids_json") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        output_ids = []
    if int(output_id) not in output_ids:
        raise HTTPException(status_code=404, detail="图片不存在")
    legacy_id = item.get("legacy_user_id")
    if legacy_id is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    row = get_output_owned(s, str(legacy_id), int(output_id))
    if not row:
        raise HTTPException(status_code=404, detail="图片不存在")
    return _serve_output_row(row, s)


def _admin_image_refund_action(review_code: str, user: UserSession, s: Settings, action: str, note: str = "") -> dict[str, Any]:
    _require_admin(user, s)
    code = review_code.upper()
    item = get_image_refund_admin(s, code)
    if not item:
        raise HTTPException(status_code=404, detail="审核记录不存在")
    current_status = str(item.get("status") or "")
    refunded_statuses = {"refunded", "refund_completed"}
    rejected_statuses = {"manual_rejected", "refund_rejected"}
    final_statuses = refunded_statuses | rejected_statuses
    if current_status in refunded_statuses:
        if action == "approve":
            conn = connect(s)
            try:
                record_admin_audit(conn, user.user_id, str(item.get("account_id") or ""), "image_refund_approve_already_refunded")
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "item": item, "refund": {"refunded": False, "already_refunded": True, "amount": int(item.get("charged_credits") or 0)}}
        conn = connect(s)
        try:
            record_admin_audit(conn, user.user_id, str(item.get("account_id") or ""), f"image_refund_{action}_final_state_locked")
            conn.commit()
        finally:
            conn.close()
        raise HTTPException(status_code=409, detail="final_state_locked")
    if current_status in rejected_statuses:
        if action == "reject":
            conn = connect(s)
            try:
                record_admin_audit(conn, user.user_id, str(item.get("account_id") or ""), "image_refund_reject_already_rejected")
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "item": item, "already_rejected": True}
        conn = connect(s)
        try:
            record_admin_audit(conn, user.user_id, str(item.get("account_id") or ""), f"image_refund_{action}_final_state_locked")
            conn.commit()
        finally:
            conn.close()
        raise HTTPException(status_code=409, detail="final_state_locked")
    if current_status == "refund_pending" and action != "approve":
        raise HTTPException(status_code=409, detail="final_state_locked")
    if action == "approve":
        if current_status == "refund_pending":
            return {"ok": True, "item": item, "refund": {"refunded": False, "already_refunded": False, "amount": int(item.get("charged_credits") or 0)}}
        set_image_refund_status(s, code, "refund_pending", public_reason=note or "管理员人工批准退款。")
        try:
            refund = refund_image_review_atomic(s, code, user.user_id)
        except Exception as exc:
            set_image_refund_status(s, code, "refund_pending", public_reason="退款暂未完成,将稍后重试。")
            raise HTTPException(status_code=409, detail="退款暂未完成,请稍后重试") from exc
        final_item = get_image_refund_admin(s, code) or item
        audit_action = "image_refund_approve"
        result = {"ok": True, "item": final_item, "refund": refund}
    elif action == "reject":
        manual_attempted = int(item.get("manual_review_attempts") or 0) > 0 or current_status in {
            "manual_review_requested",
            "manual_reviewing",
        }
        if manual_attempted:
            final_item = reject_manual_image_refund_review(
                s,
                code,
                operator_id=user.user_id,
                public_reason=note or "人工复审未通过,不能再次申请退款。",
            )
            audit_action = "image_refund_manual_reject"
        else:
            final_item = set_image_refund_status(s, code, "auto_rejected", public_reason=note or "管理员人工拒绝退款。")
            audit_action = "image_refund_reject"
        result = {"ok": True, "item": final_item}
    elif action == "manual_review":
        final_item = set_image_refund_status(s, code, "manual_review_available", public_reason=note or "可提交人工复审。")
        audit_action = "image_refund_manual_review"
        result = {"ok": True, "item": final_item}
    elif action == "retry":
        final_item = set_image_refund_status(s, code, "pending", public_reason=note or "")
        audit_action = "image_refund_retry"
        result = {"ok": True, "item": final_item}
    else:
        raise HTTPException(status_code=400, detail="未知操作")
    conn = connect(s)
    try:
        record_admin_audit(conn, user.user_id, str(item.get("account_id") or ""), audit_action)
        conn.commit()
    finally:
        conn.close()
    return result


@app.post("/api/admin/image-refunds/{review_code}/approve")
def admin_image_refund_approve(
    review_code: str,
    body: AdminImageRefundActionRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _admin_image_refund_action(review_code, user, s, "approve", body.note or "")


@app.post("/api/admin/image-refunds/{review_code}/reject")
def admin_image_refund_reject(
    review_code: str,
    body: AdminImageRefundActionRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _admin_image_refund_action(review_code, user, s, "reject", body.note or "")


@app.post("/api/admin/image-refunds/{review_code}/retry")
def admin_image_refund_retry(
    review_code: str,
    body: AdminImageRefundActionRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _admin_image_refund_action(review_code, user, s, "retry", body.note or "")


@app.post("/api/admin/image-refunds/{review_code}/manual-review")
def admin_image_refund_manual(
    review_code: str,
    body: AdminImageRefundActionRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    return _admin_image_refund_action(review_code, user, s, "manual_review", body.note or "")



@app.get("/api/topups")
def list_topups(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    items = list_recharge_requests_for_user(s.balance_db, legacy_id, limit=20)
    return {"items": [serialize_topup(item, s) for item in items]}


@app.post("/api/topups")
def create_topup(
    body: TopupCreateRequest,
    user: UserSession = Depends(get_current_user),
    csrf: None = Depends(require_csrf),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    try:
        payment_method = normalize_payment_method(body.payment_method)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Check for existing pending order of the same payment method
    existing = find_pending_topup_for_user(s.balance_db, legacy_id, payment_method=payment_method)
    if existing is not None:
        existing_status = existing["status"]
        item = serialize_topup(existing, s)
        if existing_status == "paid":
            return {
                "status": "existing_pending",
                "payment_code": existing["code"],
                "message": "该充值订单正在等待管理员审核,无需重复提交。",
                "item": item,
            }
        else:
            return {
                "status": "existing_pending",
                "payment_code": existing["code"],
                "message": "你有一笔待处理充值订单,请继续查看。",
                "item": item,
            }

    try:
        amount_fen = None
        credits = None
        if payment_method == PAYMENT_METHOD_ASB:
            if not s.asb_transfer_enabled or not s.asb_payee_name.strip() or not s.asb_account_number.strip():
                raise ValueError("ASB payment is temporarily unavailable. Please contact the admin.")
            credits = validate_asb_credits(body.credits)
        else:
            amount_fen = parse_rmb_to_fen(body.amount_rmb or "")
        code = create_recharge_request_for_identity(
            s.balance_db,
            user_id=legacy_id,
            username=user.username,
            amount_fen=amount_fen,
            source="web",
            payment_method=payment_method,
            credits=credits,
            asb_enabled=s.asb_transfer_enabled,
            wechat_expires_hours=s.topup_wechat_expires_hours,
            asb_expires_days=s.topup_asb_expires_days,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    order = get_recharge_order(s.balance_db, code)
    return {
        "status": "created",
        "payment_code": code,
        "item": serialize_topup(order, s),
    }


@app.get("/api/topups/{code}")
def get_topup(code: str, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    order = get_recharge_order(s.balance_db, code)
    if not order or str(order["user_id"]) != str(legacy_id):
        raise HTTPException(status_code=404, detail="充值订单不存在")
    return {"item": serialize_topup(order, s)}


@app.post("/api/topups/{code}/paid")
def mark_topup_paid(
    code: str,
    user: UserSession = Depends(get_current_user),
    csrf: None = Depends(require_csrf),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    ok, msg = mark_recharge_paid_for_user(
        s.balance_db,
        code,
        legacy_id,
        wechat_expires_hours=s.topup_wechat_expires_hours,
        asb_expires_days=s.topup_asb_expires_days,
        paid_review_days=s.topup_paid_review_days,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    order = get_recharge_order(s.balance_db, code)
    return {"ok": True, "message": msg, "item": serialize_topup(order, s)}


@app.post("/api/topups/{code}/cancel")
def cancel_topup(
    code: str,
    user: UserSession = Depends(get_current_user),
    csrf: None = Depends(require_csrf),
    s: Settings = Depends(get_settings),
):
    """Cancel a recharge request that is in 'created' status.

    Only 'created' orders can be cancelled by the user.
    'paid'/'approved'/'rejected'/'expired' orders cannot be cancelled.
    """
    legacy_id = get_legacy_user_id_for_session(user, s)
    ok, msg = cancel_recharge_request(s.balance_db, code, legacy_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@app.get("/api/payment-qr")
def payment_qr(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    path = s.payment_qr_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="收款码暂未配置")
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


def serialize_topup(order: dict | None, s: Settings) -> dict:
    if not order:
        return {}
    amount_fen = int(order["amount_fen"])
    method = normalize_payment_method(order.get("payment_method"))
    credits = int(order.get("credits") or amount_fen)
    item = {
        "code": order["code"],
        "amount_fen": amount_fen,
        "amount_text": topup_amount_text(order),
        "payment_method": method,
        "payment_method_label": payment_method_label(order),
        "payment_reference": order.get("payment_reference") or order["code"],
        "currency": order.get("currency") or ("NZD" if method == PAYMENT_METHOD_ASB else "RMB"),
        "credits": credits,
        "credits_text": topup_credit_text(order),
        "status": order["status"],
        "created_at": order["created_at"],
        "paid_at": order.get("paid_at"),
        "reviewed_at": order.get("reviewed_at"),
        "expires_at": order.get("expires_at"),
        "paid_expires_at": order.get("paid_expires_at"),
        "source": order.get("source") or "discord",
    }
    if method == PAYMENT_METHOD_ASB:
        item["asb"] = {
            "enabled": bool(s.asb_transfer_enabled),
            "configured": bool(s.asb_transfer_enabled and s.asb_payee_name.strip() and s.asb_account_number.strip()),
            "payee_name": s.asb_payee_name,
            "bank_name": s.asb_bank_name,
            "account_number": s.asb_account_number,
            "packages": [
                {"credits": c, "amount_text": f"NZD ${amount}"}
                for c, amount in ASB_CREDIT_PACKAGES.items()
            ],
        }
    else:
        payment_link = str(s.wechat_payment_link or "").strip()
        if payment_link:
            item["wechat_payment_link"] = payment_link
    return item
@app.get("/api/settings")
def get_settings_api(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    row = get_user_settings_row(s, legacy_id)
    if not row:
        return {
            "style_key": "style_a",
            "lora_weight": 1.0,
            "last_width": 1024,
            "last_height": 1536,
        }
    style_key = row.get("style_key") or "style_a"
    if style_key == "anima":
        style_key = "anima_owner"
    return {
        "style_key": style_key,
        "lora_weight": row.get("lora_weight", 1.0),
        "last_width": row.get("last_width") or 1024,
        "last_height": row.get("last_height") or 1536,
    }


@app.put("/api/settings")
def put_settings(
    style_key: str = Form(...),
    lora_weight: float = Form(...),
    width: int = Form(...),
    height: int = Form(...),
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if style_key == "anima":
        style_key = "anima_owner"
    legacy_id = get_legacy_user_id_for_session(user, s)
    try:
        saved = save_user_settings(
            s, legacy_id,
            style_key=style_key,
            lora_weight=lora_weight,
            width=width,
            height=height,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "settings": saved}


@app.get("/api/catalog")
def catalog(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    styles = [
        item for item in STYLES
        if not item.get("hidden") and (not item.get("owner_only") or is_admin_user(user, s))
    ]
    return {"styles": styles, "control_characters": CONTROL_CHARACTERS}


@app.get("/api/queue/status")
def queue_status(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    redis_key = "uma:cache:queue_status"
    redis_cached = cache_get_json(s, redis_key)
    if redis_cached is not None:
        return redis_cached
    now = time.monotonic()
    cached = _queue_status_cache.get("data")
    if cached is not None and float(_queue_status_cache.get("expires_at") or 0) > now:
        return cached
    data = get_queue_status(s)
    has_active = any(int(data.get(key, 0) or 0) > 0 for key in ("smart_planning_count", "queued_total", "translating_count", "processing_count"))
    ttl = 2 if has_active else 8
    _queue_status_cache["data"] = data
    _queue_status_cache["expires_at"] = now + min(QUEUE_STATUS_CACHE_SECONDS, ttl)
    cache_set_json(s, redis_key, data, ttl)
    return data


@app.get("/api/tasks")
def tasks(
    status: str = "all",
    limit: int = 20,
    offset: int = 0,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    if status.lower() in {"active", "completed", "all"}:
        items, has_more = list_user_tasks_filtered(s, legacy_id, status_filter=status, limit=limit, offset=offset)
        return {"items": items, "has_more": has_more}
    return {"items": list_user_tasks(s, legacy_id)}


@app.get("/api/tasks/summary")
def tasks_summary(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    redis_key = f"{TASK_SUMMARY_CACHE_PREFIX}:{legacy_id}"
    cached = cache_get_json(s, redis_key)
    if cached is not None:
        return cached
    data = get_user_task_summary(s, legacy_id)
    ttl = 2 if int(data.get("active_count", 0) or 0) > 0 else 8
    cache_set_json(s, redis_key, data, ttl)
    return data


@app.get("/api/smart-agent/config")
def smart_agent_config(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    return {
        "enabled": bool(s.smart_agent_enabled),
        "cost_credits": int(s.smart_agent_cost_credits),
        "image_only": True,
        "v2_enabled": bool(getattr(s, "smart_agent_v2_enabled", False)),
    }


def _rate_limit_smart_agent(request: Request, user: UserSession, s: Settings, *, action: str) -> None:
    if action not in {"chat", "generate"}:
        action = "chat"
    window = max(1, int(s.smart_agent_rate_window_seconds or 600))
    limit_type = f"smart_agent_{action}"
    user_hash = hashlib.sha256(str(user.user_id).encode("utf-8")).hexdigest()[:16]
    ip_raw = (request.client.host if request.client else "") or "unknown"
    ip_hash = hashlib.sha256(ip_raw.encode("utf-8")).hexdigest()[:16]
    if action == "chat":
        emergency_window = max(1, int(s.smart_agent_chat_emergency_window_seconds or 600))
        emergency_limit = max(1, int(s.smart_agent_chat_emergency_ip_limit or 300))
        redis_ip = incr_with_ttl(s, f"uma:smart_agent:rate:chat:emergency_ip:{ip_hash}", emergency_window)
        if redis_ip is not None and redis_ip > emergency_limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "generation_rate_limited",
                    "message": "请求过于频繁,请稍后再试。",
                    "retry_after": emergency_window,
                    "limit_type": "smart_agent_chat_emergency",
                },
                headers={"Retry-After": str(emergency_window)},
            )
        if redis_ip is None:
            limiter.check(
                f"smart-agent:chat:emergency-ip:{ip_hash}",
                emergency_limit,
                emergency_window,
                limit_type="smart_agent_chat_emergency",
            )
        return

    user_limit = int(s.smart_agent_generate_user_limit)
    ip_limit = int(s.smart_agent_generate_ip_limit)
    redis_user = incr_with_ttl(s, f"uma:smart_agent:rate:{action}:user:{user_hash}", window)
    redis_ip = incr_with_ttl(s, f"uma:smart_agent:rate:{action}:ip:{ip_hash}", window)
    if redis_user is not None and redis_user > user_limit:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "generation_rate_limited",
                "message": "提交过于频繁,请稍后重试。",
                "retry_after": window,
                "limit_type": limit_type,
            },
            headers={"Retry-After": str(window)},
        )
    if redis_ip is not None and redis_ip > ip_limit:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "generation_rate_limited",
                "message": "提交过于频繁,请稍后重试。",
                "retry_after": window,
                "limit_type": limit_type,
            },
            headers={"Retry-After": str(window)},
        )
    if redis_user is None or redis_ip is None:
        limiter.check(f"smart-agent:{action}:user:{user_hash}", user_limit, window, limit_type=limit_type)
        limiter.check(f"smart-agent:{action}:ip:{ip_hash}", ip_limit, window, limit_type=limit_type)


@app.post("/api/smart-agent/tasks")
def create_smart_agent_task(
    payload: SmartAgentTaskRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        raise HTTPException(status_code=409, detail="Smart Agent V2 请通过聊天消息提交。")
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    if not s.deepseek_api_key:
        raise HTTPException(status_code=503, detail="Smart Agent 暂未配置,请稍后再试")
    _rate_limit_smart_agent(request, user, s, action="generate")
    legacy_id = get_legacy_user_id_for_session(user, s)
    job_code = make_job_code()
    request_text = payload.request.strip()
    prompt_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:12]
    try:
        result = create_smart_agent_task_atomic(
            s,
            job_code=job_code,
            user_id=legacy_id,
            username=user.username,
            request_text=request_text,
            cost_credits=int(s.smart_agent_cost_credits),
            client_request_id=request.headers.get("X-Client-Request-Id"),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    redis_delete(s, "uma:cache:queue_status", f"uma:cache:tasks_summary:{legacy_id}", f"{TASK_SUMMARY_CACHE_PREFIX}:{legacy_id}")
    print(
        f"[SMART_AGENT] submitted job={result['job_code']} prompt_hash={prompt_hash} "
        f"prompt_len={len(request_text)}",
        flush=True,
    )
    return {
        "ok": True,
        "job_code": result["job_code"],
        "status": result["status"],
        "charged_credits": result["charged_fen"],
        "deduped": result.get("deduped", False),
    }


# ── Smart Agent Chat & Conversation APIs ──

@app.post("/api/smart-agent/conversations")
def create_smart_agent_conversation(
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    legacy_id = get_legacy_user_id_for_session(user, s)
    account_id = int(user.user_id) if user.user_id.isdigit() else hash(user.user_id) % 10**9
    conv = create_conversation(s, legacy_user_id=legacy_id, account_id=account_id)
    add_conversation_message(
        s, conversation_id=conv["id"], role="system_event",
        content="conversation_created", safe_content="",
    )
    return {"ok": True, "conversation_code": conv["conversation_code"]}


@app.get("/api/smart-agent/conversations")
def list_smart_agent_conversations(
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    convs = list_conversations(s, legacy_user_id=legacy_id)
    return {"ok": True, "conversations": convs}


@app.get("/api/smart-agent/conversations/{conversation_code}")
def get_smart_agent_conversation(
    conversation_code: str,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = get_conversation_messages(s, conversation_id=conv["id"], limit=50)
    # Only return safe_content to frontend
    safe_messages = [
        {
            "id": m["id"],
            "role": m["role"],
            "content": m["safe_content"] or m["role"],
            "created_at": m["created_at"],
            "status": m.get("status") or "done",
            "intent": m.get("intent") or "",
            "processed_at": m.get("processed_at"),
            "error": m.get("error") or "",
        }
        for m in messages
    ]
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        for item in safe_messages:
            content = str(item.get("content") or "")
            if item.get("role") == "assistant" and (
                content.startswith("这是为你整理的英文提示词")
                or "当前 Prompt:" in content
                or "当前 prompt:" in content.lower()
            ):
                item["content"] = safe_prompt_hidden_reply()

    # Check pending disambiguation
    pending_dis = get_pending_disambiguation_json(s, conversation_id=conv["id"])
    return {
        "ok": True,
        "conversation": conv,
        "messages": safe_messages,
        "pending_disambiguation": bool(pending_dis),
    }


def _add_safe_smart_agent_event(
    s: Settings,
    *,
    conversation_id: int,
    event_type: str,
    public_message: str,
    private_detail: str = "",
    job_code: str = "",
) -> int:
    return add_conversation_event(
        s,
        conversation_id=conversation_id,
        event_type=event_type,
        public_message=sanitize_public_agent_message(public_message),
        private_detail=(private_detail or "")[:2000],
        job_code=job_code,
    )


SMART_AGENT_CHAT_INTENTS = {"chat", "generate", "regenerate", "edit"}


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _resolve_smart_agent_intent(text: str) -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    if not raw:
        return "chat"
    casual = {"hi", "hello", "hey", "你好", "在吗", "谢谢", "thanks", "thank you"}
    if lowered in casual or raw in casual:
        return "chat"

    force_chat_zh = (
        "不要生成", "别生成", "先别生成", "暂时不要生成", "不用生成", "不需要生成",
        "不要直接生成", "别直接生图", "先不出图", "我还在商量", "先商量", "先讨论",
        "只是问问", "我想问一下", "让我选一下", "给我推荐几个", "给我几个方案",
        "怎么描述", "怎么写", "怎么调整", "有什么 tag", "有什么 prompt", "解释一下",
        "先给建议", "先看看怎么设计", "我还没决定", "还没有确定", "先不要提交",
        "先不创建任务", "不要提交", "不创建任务", "别提交", "别直接生成",
    )
    force_chat_en = (
        "do not generate", "don't generate", "dont generate", "don't make it yet", "dont make it yet",
        "don't submit yet", "dont submit yet", "just asking", "let's discuss first", "lets discuss first",
        "give me suggestions", "give me some options", "how should i describe it",
        "i haven't decided yet", "i have not decided yet", "not yet", "do not submit",
    )
    if _contains_any(raw, force_chat_zh) or _contains_any(lowered, force_chat_en):
        return "chat"

    regenerate_markers = (
        "重新生成", "重生成", "再生成上一张", "重做上一张", "上一张重新", "regenerate", "rerun",
    )
    edit_markers = (
        "编辑这张", "改这张", "修改这张", "用这张图改", "edit this", "modify this", "change this image",
    )
    explicit_generate_zh = (
        "生成一张", "帮我生图", "现在生成", "开始生成", "按这个方案生成", "就按这个生成",
        "可以出图了", "确认生成", "提交生成", "直接生成吧", "直接生成", "现在出图", "开始出图",
        "生成吧", "帮我生成", "帮我出图", "给我生成", "给我生图", "给我出图",
        "生成第一个", "生成第一个方案", "生成第一個", "生成第一個方案", "生成第二个", "生成第二个方案",
        "生成第三个", "生成第三个方案", "生成第四个", "生成第四个方案",
        "按第一个方案生成", "按第一个生成", "按第一個方案生成", "按第一個生成",
        "按第二个方案生成", "按第二个生成", "按第三个方案生成", "按第三个生成",
        "按第四个方案生成", "按第四个生成", "就按刚才的方案生成", "按刚才的方案生成",
        "就按刚才生成", "可以了,出图吧", "可以了出图吧", "出图吧",
        "用第 1 个方案生成", "用第 2 个方案生成", "用第 3 个方案生成", "用第 4 个方案生成",
        "用第1个方案生成", "用第2个方案生成", "用第3个方案生成", "用第4个方案生成",
        "选第 1 个,现在生成", "选第 2 个,现在生成", "选第 3 个,现在生成", "选第 4 个,现在生成",
        "选第1个,现在生成", "选第2个,现在生成", "选第3个,现在生成", "选第4个,现在生成",
        "就这样,帮我生成", "就这样帮我生成", "就按第 1 个方案生成", "就按第 2 个方案生成",
        "就按第 3 个方案生成", "就按第 4 个方案生成", "就按第1个方案生成", "就按第2个方案生成",
        "就按第3个方案生成", "就按第4个方案生成",
    )
    explicit_generate_en = (
        "generate an image", "generate a picture", "make an image", "create an image",
        "generate now", "start generation", "submit generation", "submit it now",
        "go ahead and generate", "use option 1 and generate", "use option 2 and generate",
        "use option 3 and generate", "use option 4 and generate", "generate with option",
    )
    if _contains_any(raw, regenerate_markers) or _contains_any(lowered, regenerate_markers):
        return "regenerate"
    if _contains_any(raw, edit_markers) or _contains_any(lowered, edit_markers):
        return "edit"
    if _contains_any(raw, explicit_generate_zh) or _contains_any(lowered, explicit_generate_en):
        return "generate"
    # "生成" followed by content that is not a bare confirmation → generate request
    if raw.startswith("生成") and len(raw) > 2 and not _is_bare_generate_request(raw):
        return "generate"
    return "chat"


def _is_prompt_ready_confirmation(text: str, *, prompt_ready: bool) -> bool:
    """检测用户消息是否为 prompt_ready 确认生成。

    重要规则:
    - 如果还没有 prompt_ready 草稿(prompt_ready=False),绝不当成确认处理,
      必须走正常 chat worker 流程,避免 prompt_draft_not_ready。
    - 如果已有 prompt_ready 草稿,仅匹配纯确认短语,不含场景/角色/服装等新信息。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    lowered = raw.lower()

    # 没有草稿时:绝不当确认,统一走 worker 流程
    if not prompt_ready:
        return False

    # 极简短确认
    if raw in {"好", "可以", "行", "确认", "ok", "OK", "Ok", "嗯", "嗯嗯", "好的", "好滴"}:
        return True

    # 纯确认短语(不含新场景/角色信息)
    pure_confirmation = (
        "开始生成", "生成吧", "现在生成", "确认生成", "可以出图了", "出图吧",
        "开始出图", "提交生成", "就按这个生成", "按这个生成", "按这个方案生成",
        "嗯开始生成", "嗯,开始生成", "嗯 开始生成", "好开始生成", "好的开始生成",
        "可以开始生成", "就按这个", "按这个方案", "现在出图", "直接生成吧",
        "go ahead and generate", "generate now", "start generation", "submit generation",
        "go ahead", "try it now", "confirm and generate",
    )
    if _contains_any(raw, pure_confirmation) or _contains_any(lowered, pure_confirmation):
        return True

    return False


def _is_scene_delegation_request(text: str) -> bool:
    """检测用户是否委托 Agent 自主选择场景。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    markers = (
        "场景随便", "场景随意", "场景你定", "场景你来决定", "场景你决定",
        "随机场景", "随便选一个场景", "你来决定场景", "你决定场景",
        "你觉得合适就行", "按你推荐的来", "都可以", "自由发挥",
        "随便拍拍", "any scene is fine", "any scene", "up to you",
        "you decide the scene", "your choice", "any setting",
    )
    lowered = raw.lower()
    return _contains_any(raw, markers) or _contains_any(lowered, markers)


def _looks_like_confirmation_attempt(text: str) -> bool:
    """检测用户消息是否「看起来像是」确认生成的尝试(用于等待 draft 时的友好提示)。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    markers = (
        "开始生成", "生成吧", "现在生成", "确认生成", "可以出图了", "出图吧",
        "开始出图", "提交生成", "就按这个生成", "按这个生成", "按这个方案生成",
        "嗯开始生成", "嗯,开始生成", "嗯 开始生成", "好开始生成", "好的开始生成",
        "可以开始生成", "就按这个", "按这个方案", "现在出图", "直接生成吧",
        "go ahead", "generate now", "start generation",
    )
    lowered = raw.lower()
    if _contains_any(raw, markers) or _contains_any(lowered, markers):
        return True
    # Bare confirmations that don't contain scene/character info
    if raw in {"确认", "生成", "确认确认", "好的", "就这个", "就这样", "就它了", "这个生成", "就生成这个"}:
        return True
    return False


def _is_character_query(text: str) -> bool:
    """检测用户是否在问当前人物/场景/服装等草稿信息。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    markers = (
        "这个是啥场景", "现在是什么场景", "是什么场景", "啥场景", "什么场景",
        "穿的什么", "穿什么", "什么服装", "什么衣服",
        "现在选的是哪个人物", "哪个人物", "什么人物", "选了谁",
        "最终 Prompt", "最终提示词", "prompt 是什么", "提示词是什么",
        "整理好了吗", "弄好了吗", "怎么样了", "方案是什么",
        "看看方案", "看看提示词", "看看当前", "现在是什么方案",
    )
    return any(marker in raw for marker in markers)


def _is_prompt_text_request(text: str) -> bool:
    """Detect requests to view/copy the current English generation prompt."""
    raw = str(text or "").strip()
    lowered = raw.lower()
    if not raw:
        return False
    markers = (
        "英文提示词", "英文 prompt", "英文prompt", "最终 prompt", "最终prompt",
        "最终提示词", "生成的提示词", "整理后的提示词", "整理好的提示词",
        "把提示词发给我", "提示词发给我", "发我提示词", "给我提示词",
        "查看 prompt", "查看prompt", "看看 prompt", "看看prompt",
        "刚才生成使用的英文提示词", "刚才的英文提示词", "不生成，只发提示词",
        "english prompt", "final prompt", "generated prompt", "send me the prompt",
        "show me the prompt", "copy the prompt", "prompt only",
    )
    return _contains_any(raw, markers) or _contains_any(lowered, markers)


def _valid_saved_prompt_from_draft(draft: dict[str, Any] | None) -> str:
    if not draft:
        return ""
    prompt_text = str(draft.get("prompt_draft") or "").strip()
    if not prompt_text or prompt_text == "[character_selected]":
        return ""
    if len(prompt_text) < 8:
        return ""
    return prompt_text


def _format_prompt_text_response(prompt_text: str) -> str:
    prompt = str(prompt_text or "").strip()
    if not prompt:
        return "暂时没有可用的英文 Prompt，请先告诉我你想生成的画面，我会重新整理。"
    return f"这是为你整理的英文提示词：\n\n{prompt}"


def _build_character_query_response(draft: dict[str, Any]) -> str | None:
    """从草稿中读取当前方案信息,构建回复。"""
    if not draft:
        return None
    structured_raw = str(draft.get("structured_draft_json") or "").strip()
    structured = {}
    if structured_raw:
        try:
            structured = json.loads(structured_raw)
        except (json.JSONDecodeError, TypeError):
            structured = {}
    prompt_text = str(draft.get("prompt_draft") or "").strip()
    char_key = str(draft.get("resolved_character_key") or "").strip()
    parts = []
    # 人物
    if char_key:
        for c in load_characters():
            if stable_character_key(c) == char_key:
                parts.append(f"当前人物:{c.get('name_zh', '')} / {c.get('name_en', '')}")
                break
    # 场景
    scene = str(structured.get("scene") or "").strip()
    if scene:
        parts.append(f"当前场景:{scene}")
    # 服装
    clothing = str(structured.get("clothing") or "").strip()
    if clothing:
        parts.append(f"当前服装:{clothing}")
    # 风格
    style = str(structured.get("style") or "").strip()
    if style:
        parts.append(f"当前风格:{style}")
    # Prompt
    if prompt_text:
        parts.append(f"当前 Prompt:{prompt_text[:200]}{'...' if len(prompt_text) > 200 else ''}")
    if not parts:
        return None
    return "\n".join(parts) + "\n\n需要修改哪个方面?直接告诉我就好喵~"


def _smart_agent_draft_context_text(draft: dict[str, Any] | None) -> str:
    """Build a compact, private context string from the current prompt draft."""
    if not draft:
        return ""
    parts: list[str] = []
    request_text = str(draft.get("request_text") or "").strip()
    if request_text and request_text not in {"chat", "generate", "regenerate", "edit"}:
        parts.append(f"上一版用户需求:{request_text[:600]}")
    structured_raw = str(draft.get("structured_draft_json") or "").strip()
    if structured_raw:
        try:
            structured = json.loads(structured_raw)
        except Exception:
            structured = {}
        if isinstance(structured, dict):
            structured_parts = []
            for key in ("scene", "style", "clothing", "expression", "action", "composition", "mood"):
                value = str(structured.get(key) or "").strip()
                if value:
                    structured_parts.append(f"{key}:{value[:160]}")
            if structured_parts:
                parts.append("上一版结构化方案:" + "; ".join(structured_parts))
    plan_raw = str(draft.get("plan_json") or "").strip()
    if plan_raw:
        try:
            plan = json.loads(plan_raw)
        except Exception:
            plan = {}
        if isinstance(plan, dict):
            previous_request = str(plan.get("previous_request_text") or "").strip()
            if previous_request and previous_request != request_text:
                parts.append(f"上一版原始需求:{previous_request[:600]}")
            previous_structured = str(plan.get("previous_structured_draft_json") or "").strip()
            if previous_structured and previous_structured != structured_raw:
                try:
                    previous = json.loads(previous_structured)
                except Exception:
                    previous = {}
                if isinstance(previous, dict):
                    previous_parts = []
                    for key in ("scene", "style", "clothing", "expression", "action", "composition", "mood"):
                        value = str(previous.get(key) or "").strip()
                        if value:
                            previous_parts.append(f"{key}:{value[:160]}")
                    if previous_parts:
                        parts.append("上一版备用结构化方案:" + "; ".join(previous_parts))
    return "\n".join(dict.fromkeys(parts))


def _clean_foreign_character_tags(prompt: str, character_key: str) -> str:
    """清理最终 Prompt 中与已确认人物无关的人物身份 tag。

    只保留已确认人物的 canonical identity tags,删除其他人物的身份 tags。
    """
    if not prompt or not character_key:
        return prompt
    selected = None
    for c in load_characters():
        if stable_character_key(c) == character_key:
            selected = c
            break
    if not selected:
        return prompt
    cleaned, removed = remove_foreign_character_tags(prompt, selected_character=selected)
    return cleaned


def _clean_foreign_character_tags_multi(prompt: str, characters: list[dict[str, Any]]) -> str:
    """多人版外来人物tag清理 - 使用全库索引过滤。

    对于每个 tag，检查是否属于任何已选人物的身份/作品/分类 tag。
    如果 tag 存在于人物库但不在已选人物集合中 → 删除。
    """
    if not prompt or not characters:
        return prompt

    from .smart_agent.character_search import build_global_identity_index
    try:
        index = build_global_identity_index()
        all_foreign = index["all_foreign_tags"]
    except Exception:
        return prompt

    # 收集已选人物的所有允许tag
    allowed: set[str] = set()
    for c in characters:
        tags_str = str(c.get("tags") or "")
        for tag in _split_prompt_tags(tags_str):
            allowed.add(tag.lower().replace(" ", "_").strip("_()"))

    kept: list[str] = []
    removed = 0
    for tag in _split_prompt_tags(prompt):
        tag_key = tag.lower().replace(" ", "_").strip("_()")
        # 1. 已知外人物Tag过滤
        if tag_key in all_foreign and tag_key not in allowed:
            removed += 1
            continue
        # 2. 不在已知索引中但看起来像结构化人物身份Tag — 同样过滤
        if tag_key not in allowed and _looks_like_identity_tag(tag):
            removed += 1
            continue
        kept.append(tag)

    if removed > 0:
        _smart_trace("foreign_tags_filtered_multi", removed_count=removed)
    return ", ".join(kept)


def _find_message_by_client_request_id(settings: Settings, *, conversation_id: int, client_request_id: str) -> dict[str, Any] | None:
    """根据 client_request_id 查找已有消息(幂等检查)。"""
    if not client_request_id or not client_request_id.strip():
        return None
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT id, content, intent, status FROM smart_agent_messages "
            "WHERE conversation_id=? AND client_request_id=? ORDER BY id ASC LIMIT 1",
            (int(conversation_id), str(client_request_id).strip()[:80]),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _is_prompt_ready_continue(text: str) -> bool:
    raw = str(text or "").strip()
    lowered = raw.lower()
    markers = (
        "继续修改", "再改", "先不生成", "先别生成", "我再改改", "还要调整",
        "continue editing", "keep editing", "not yet", "don't generate yet", "do not generate yet",
    )
    return _contains_any(raw, markers) or _contains_any(lowered, markers)


def _is_local_generation_request(text: str, characters: list[dict[str, Any]] | None = None, snippets: list[dict[str, Any]] | None = None) -> bool:
    return _resolve_smart_agent_intent(text) in {"generate", "regenerate", "edit"}


def _looks_like_agent_refusal(text: str) -> bool:
    lowered = str(text or "").lower()
    markers = (
        "不符合我的生成原则", "不符合生成原则", "无法帮助", "不能帮助", "不能生成", "拒绝", "抱歉",
        "i can't", "i cannot", "cannot assist", "not able to help", "content policy", "policy", "safety",
    )
    return any(marker in lowered for marker in markers)


def _looks_like_task_execution_claim(text: str) -> bool:
    lowered = str(text or "").lower()
    markers = (
        "已创建任务", "已经创建任务", "已提交", "已经提交", "提交成功", "正在生成", "开始生成了",
        "任务已加入队列", "稍后就能看到", "后端会自动创建", "已经进入队列", "已进入队列",
        "created the task", "task has been created", "submitted", "queued", "generation has started",
        "为你生成", "正在为你", "正在准备生成", "马上出图", "即将生成", "即将出图",
        "已扣除", "马上就能看到", "已开始出图", "已经生成",
    )
    return any(marker in lowered for marker in markers)


def _server_task_reply_for_request(text: str) -> str:
    raw = str(text or "")
    if any(marker in raw for marker in ("第一个", "第一個", "第 1", "第1")):
        return "已按第一个方案提交生成。"
    if any(marker in raw for marker in ("第二个", "第二個", "第 2", "第2")):
        return "已按第二个方案提交生成。"
    if any(marker in raw for marker in ("第三个", "第三個", "第 3", "第3")):
        return "已按第三个方案提交生成。"
    if any(marker in raw for marker in ("第四个", "第四個", "第 4", "第4")):
        return "已按第四个方案提交生成。"
    if "刚才" in raw or "这个" in raw:
        return "已按当前方案提交生成。"
    return "已提交生成任务。"


def _is_bare_generate_request(text: str) -> bool:
    raw = str(text or "").strip()
    normalized = re.sub(r"[\s。!,\、吧呢喵~.]+", "", raw)
    bare_values = {
        "给我生成", "帮我生成", "帮我生图", "给我生图", "给我出图", "帮我出图",
        "现在生成", "开始生成", "生成吧", "出图吧", "提交生成", "可以出图了",
    }
    if normalized in bare_values:
        return True
    return normalized in {"生成", "生图", "出图"}


def _result_has_generation_details(
    *,
    result: dict[str, Any],
    characters: list[dict[str, Any]],
    snippets: list[dict[str, Any]],
    memory: str,
    recent_messages: list[dict[str, Any]],
) -> bool:
    """判定是否有足够的生成细节使方案达到 prompt_ready。
    
    仅有人物（characters）不算生成就绪。必须有至少一项有效的非人物
    生成细节（场景/服装/动作/表情/构图/氛围/光照/具体风格等）。
    """
    # snippets 中的 tag 也算有效内容
    if snippets:
        meaningful = _count_meaningful_non_character_tags(
            result=result,
            snippets=snippets,
        )
        if meaningful > 0:
            return True
        # snippets 存在但无可用的 non-character tag → 继续检查
    useful_keys = ("draft_prompt", "scene", "style", "clothing", "expression", "action", "composition", "mood")
    meaningful_count = _count_meaningful_non_character_tags(
        result=result,
        snippets=snippets,
    )
    if meaningful_count > 0:
        return True
    if len(str(memory or "").strip()) >= 48:
        # memory 必须够长（说明多轮积累了场景细节）才算有效
        useful_text = " ".join(
            str(result.get(key) or "")
            for key in useful_keys
            if str(result.get(key) or "").strip() and not _looks_like_agent_refusal(str(result.get(key) or ""))
        ).strip()
        if len(useful_text) >= 12:
            return True
    recent_text = " ".join(
        str(message.get("content") or message.get("safe_content") or "")
        for message in recent_messages[-8:]
        if message.get("role") in {"user", "assistant"}
    )
    visual_markers = ("场景", "服装", "表情", "动作", "构图", "光线", "氛围", "pose", "outfit", "expression")
    return len(recent_text.strip()) >= 48 and any(marker in recent_text.lower() for marker in visual_markers)


# ── Non-character tag quality helpers ──

_MEANINGLESS_FILLER_TAGS: set[str] = {
    "anime style", "masterpiece", "best quality", "high quality",
    "solo", "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
    "looking at viewer", "simple background", "white background",
    "portrait", "standing",
}
_MEANINGLESS_FILLER_PREFIXES: tuple[str, ...] = (
    "absurdres", "newest", "intricate", "detailed", "ultra detailed",
)


def _is_meaningful_non_character_tag(tag: str) -> bool:
    """判断单个 tag 是否是「真正描述画面」的非人物标签。"""
    key = tag.strip().lower().replace(" ", "_")
    if not key or len(key) <= 2:
        return False
    if key in _MEANINGLESS_FILLER_TAGS:
        return False
    if key.startswith(_MEANINGLESS_FILLER_PREFIXES):
        return False
    return True


def _count_meaningful_non_character_tags(
    *,
    result: dict[str, Any],
    snippets: list[dict[str, Any]],
) -> int:
    """统计 result 中有多少非人物有效画面标签。
    
    排除：质量标签、人数标签、anime style、纯人物身份 tag。
    计入：场景、具体风格、服装、动作、表情、构图、氛围、光线等。
    """
    count = 0
    for key in ("scene", "style", "clothing", "expression", "action", "composition", "mood"):
        value = str(result.get(key) or "")
        if not value or _looks_like_agent_refusal(value):
            continue
        for tag in _split_prompt_tags(value):
            if _is_meaningful_non_character_tag(tag):
                count += 1
    # 也计入 snippets 中的非人物标签
    for snippet in snippets[:5]:
        for tag in _split_prompt_tags(str(snippet.get("tags") or "")):
            if _is_meaningful_non_character_tag(tag):
                count += 1
        for tag in _split_prompt_tags(str(snippet.get("prompt") or "")):
            if _is_meaningful_non_character_tag(tag):
                count += 1
    return count


def _split_prompt_tags(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").replace(",", ",").split(","):
        tag = " ".join(raw.strip().split())
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _tags_from_snippets(snippets: list[dict[str, Any]], request_text: str = "") -> list[str]:
    tags: list[str] = []
    lowered_request = str(request_text or "").lower()
    allow_impossible_dress = any(
        marker in lowered_request
        for marker in ("impossible dress", "impossible_dress", "不可能服装", "幻想服装", "特殊裙装")
    )
    for item in snippets[:5]:
        if not _snippet_explicitly_requested(item, request_text):
            continue
        for tag in _split_prompt_tags(str(item.get("tags") or "")) + _split_prompt_tags(str(item.get("prompt") or "")):
            key = tag.lower().replace(" ", "_")
            if key == "impossible_dress" and not allow_impossible_dress:
                continue
            tags.append(tag)
    return tags[:60]


def _snippet_explicitly_requested(item: dict[str, Any], request_text: str) -> bool:
    raw = str(request_text or "")
    lowered = raw.lower()
    compact = re.sub(r"\s+", "", lowered)
    for field in ("scene", "style", "notes"):
        value = str(item.get(field) or "").strip()
        if not value:
            continue
        value_lower = value.lower()
        value_compact = re.sub(r"\s+", "", value_lower)
        if len(value_compact) >= 2 and (value_lower in lowered or value_compact in compact):
            return True
    for tag in _split_prompt_tags(str(item.get("tags") or "")):
        key = tag.lower().replace("_", " ").strip()
        if len(key) >= 4 and key in lowered:
            return True
    return False


def _dedupe_character_matches(characters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for character in characters:
        key = stable_character_key(character) or hashlib.sha256(
            str(character.get("tags") or character.get("name_en") or character.get("name_zh") or "").encode("utf-8")
        ).hexdigest()[:12]
        if key in seen:
            continue
        clean = dict(character)
        clean["key"] = key
        result.append(clean)
        seen.add(key)
    return result


def _fallback_tags_from_request(text: str) -> list[str]:
    raw = str(text or "")
    lowered = raw.lower()
    tags: list[str] = ["anime style"]
    mapping = [
        (("校园", "school", "campus"), ["school", "campus"]),
        (("教室", "classroom"), ["classroom"]),
        (("摄影棚", "studio"), ["photo studio", "studio lighting"]),
        (("写真", "portrait", "photo"), ["portrait"]),
        (("私人", "私房", "private"), ["private setting"]),
        (("成人", "成年", "adult", "mature"), ["adult", "mature"]),
        (("非露骨", "non-explicit", "tasteful"), ["non-explicit", "tasteful"]),
        (("女孩", "girl"), ["1girl"]),
        (("男孩", "boy"), ["1boy"]),
        (("全身", "full body"), ["full body"]),
        (("半身", "cowboy"), ["cowboy shot"]),
        (("特写", "close"), ["close-up"]),
        (("微笑", "smile"), ["smile"]),
        (("看镜头", "looking at viewer"), ["looking at viewer"]),
        (("阳光", "sunlight"), ["sunlight"]),
        (("柔光", "soft"), ["soft lighting"]),
    ]
    haystack = lowered + " " + raw
    for needles, values in mapping:
        if any(needle in haystack for needle in needles):
            tags.extend(values)
    return tags


_PROMPT_HARD_CONSTRAINT_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("body_large_bust", ("大胸", "胸大", "巨乳", "丰满胸部", "胸部丰满", "突出胸大", "胸部明显", "large breasts", "large bust", "full bust"), ("large breasts", "large bust", "emphasized bust", "prominent bust", "full bust")),
    ("body_slender", ("纤细", "苗条", "slender"), ("slender",)),
    ("body_long_legs", ("长腿", "long legs"), ("long legs",)),
    ("body_tall", ("高挑", "高个", "tall"), ("tall",)),
    ("body_petite", ("娇小", "petite"), ("petite",)),
    ("clothing_tight", ("紧身", "贴身", "修身", "form-fitting", "tight"), ("tight clothing", "form-fitting")),
    ("clothing_white_short_sleeve", ("白色短袖", "白短袖", "white short sleeve", "white t-shirt"), ("white short sleeve shirt", "white shirt", "short sleeves")),
    ("clothing_school_uniform", ("校服", "school uniform"), ("school uniform",)),
    ("clothing_sportswear", ("运动服", "sportswear"), ("sportswear",)),
    ("clothing_swimsuit", ("泳装", "泳衣", "swimsuit"), ("swimsuit",)),
    ("clothing_jacket", ("外套", "jacket"), ("jacket",)),
    ("pose_standing", ("站着", "站在", "standing"), ("standing",)),
    ("pose_sitting", ("坐着", "坐在", "sitting"), ("sitting",)),
    ("pose_kneeling", ("蹲下", "跪着", "kneeling"), ("kneeling",)),
    ("pose_lying", ("躺着", "lying"), ("lying",)),
    ("view_looking_at_viewer", ("看镜头", "看着镜头", "looking at viewer"), ("looking at viewer",)),
    ("view_back_view", ("背对镜头", "back view"), ("back view",)),
    ("expression_annoyed", ("嫌弃", "一脸嫌弃", "嫌弃地看着", "disdainful", "annoyed", "unimpressed"), ("annoyed expression", "disdainful look", "unimpressed expression")),
    ("expression_shy", ("害羞", "shy"), ("shy expression", "blush")),
    ("expression_angry", ("生气", "angry"), ("angry expression", "annoyed expression")),
    ("expression_expressionless", ("无表情", "冷淡", "不要笑", "expressionless", "cold expression"), ("cold expression", "expressionless", "neutral expression")),
    ("expression_crying", ("哭泣", "哭", "crying"), ("crying",)),
    ("scene_bedroom", ("卧室", "bedroom"), ("bedroom", "indoor")),
    ("scene_classroom", ("教室", "classroom"), ("classroom", "indoor")),
    ("scene_park", ("公园", "park"), ("park", "outdoor")),
    ("scene_night_street", ("夜晚街道", "night street"), ("night street", "night")),
    ("scene_bathroom", ("浴室", "bathroom"), ("bathroom", "indoor")),
    ("scene_beach", ("海边", "海滩", "beach"), ("beach", "outdoor")),
    ("composition_upper_body", ("上半身", "upper body"), ("upper body",)),
    ("composition_cowboy_shot", ("半身", "cowboy shot"), ("cowboy shot",)),
    ("composition_full_body", ("全身", "full body"), ("full body",)),
    ("composition_front_view", ("正面视角", "正面", "front view"), ("front view",)),
    ("composition_low_angle", ("低角度", "low angle"), ("low angle",)),
    ("composition_from_above", ("俯视", "高机位", "from above"), ("from above", "high angle")),
    ("composition_closeup", ("近景", "特写", "close-up"), ("close-up",)),
)

_PROMPT_CONFLICT_TAGS_BY_RULE: dict[str, tuple[str, ...]] = {
    "expression_annoyed": ("smile", "happy expression", "cute"),
    "expression_angry": ("smile", "happy expression", "cute"),
    "expression_expressionless": ("smile", "happy expression", "cute"),
}

_PROMPT_TAGS_REQUIRING_EXPLICIT_REQUEST: dict[str, tuple[str, ...]] = {
    "impossible_dress": ("impossible dress", "impossible_dress", "不可能服装", "幻想服装", "特殊裙装"),
}


def _extract_prompt_hard_constraint_tags(user_text: str) -> tuple[list[str], set[str]]:
    raw = str(user_text or "")
    lowered = raw.lower()
    compact = lowered.replace(" ", "")
    required: list[str] = []
    forbidden: set[str] = set()
    seen: set[str] = set()
    for rule_id, patterns, tags in _PROMPT_HARD_CONSTRAINT_RULES:
        matched = False
        for pattern in patterns:
            p = str(pattern).lower()
            if (p and p in lowered) or (p.replace(" ", "") and p.replace(" ", "") in compact):
                matched = True
                break
        if not matched:
            continue
        for tag in tags:
            key = tag.lower()
            if key not in seen:
                required.append(tag)
                seen.add(key)
        for conflict in _PROMPT_CONFLICT_TAGS_BY_RULE.get(rule_id, ()):
            forbidden.add(conflict.lower())
    return required, forbidden


def _tag_present(tags: list[str], expected_tag: str) -> bool:
    expected_key = expected_tag.lower().replace("_", " ").strip()
    expected_compact = expected_key.replace(" ", "")
    for tag in tags:
        key = tag.lower().replace("_", " ").strip()
        if key == expected_key or key.replace(" ", "") == expected_compact:
            return True
    return False


def _apply_prompt_core_fidelity(
    prompt: str,
    user_text: str,
    *,
    protected_tags: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Keep explicit user visual requirements in the final booru-style prompt."""
    required, forbidden = _extract_prompt_hard_constraint_tags(user_text)
    protected_keys = {tag.lower().replace("_", " ").strip() for tag in (protected_tags or [])}
    request_lower = str(user_text or "").lower()
    kept: list[str] = []
    removed: list[str] = []
    for tag in _split_prompt_tags(prompt):
        key = tag.lower().replace("_", " ").strip()
        compact_key = key.replace(" ", "_")
        if key in protected_keys:
            kept.append(tag)
            continue
        if key in forbidden:
            removed.append(tag)
            continue
        explicit_needles = _PROMPT_TAGS_REQUIRING_EXPLICIT_REQUEST.get(compact_key)
        if explicit_needles and not any(str(needle).lower() in request_lower for needle in explicit_needles):
            removed.append(tag)
            continue
        kept.append(tag)

    added: list[str] = []
    for tag in required:
        if not _tag_present(kept, tag):
            kept.append(tag)
            added.append(tag)

    return ", ".join(kept[:180]), {
        "required": required,
        "added": added,
        "removed": removed,
        "forbidden": sorted(forbidden),
    }


def _local_resolution_key(result: dict[str, Any], request_text: str) -> str:
    hint = str(result.get("resolution_hint") or result.get("resolution_key") or "").strip()
    if hint in ALLOWED_RESOLUTIONS:
        return hint
    lowered = f"{hint} {request_text}".lower()
    if any(item in lowered for item in ("1:1", "square", "头像", "icon", "pfp", "正方")):
        return "square_1024"
    if any(item in lowered for item in ("landscape", "横", "风景", "horizontal", "3:2")):
        return "landscape_1536x1024"
    if any(item in lowered for item in ("vertical", "portrait", "竖", "手机壁纸", "海报", "2:3")):
        return "portrait_1024x1536"
    return DEFAULT_RESOLUTION_KEY


def _build_selected_characters_json(characters: list[dict[str, Any]]) -> str:
    """将匹配人物列表转换为 selected_characters_json 字符串。

    这是人物状态的唯一权威来源。所有人物读取必须从此字段获取。
    """
    import json as _json
    result = []
    for c in characters:
        identity_key = str(c.get("character_key") or c.get("key") or "")
        source = str(c.get("character_tag_source") or c.get("source") or "character_library")
        canonical_tags = _split_prompt_tags(str(c.get("tags") or ""))
        # 对于 explicit_user_character / agent_fallback 人物，将用户明确输入的 Tag
        # 也加入 canonical_tags，确保后续过滤不会误删用户选择的库外人物 Tag。
        if source in ("agent_fallback", "explicit_user_character", "explicit_user_tag"):
            explicit_name = str(c.get("translated_character_name") or c.get("name_en") or "").strip()
            original_name = str(c.get("original_character_name") or "").strip()
            for name_candidate in (original_name, explicit_name):
                if name_candidate and name_candidate not in canonical_tags:
                    canonical_tags.append(name_candidate)
            # 也加入 explicit_tags 列表（如果存在）
            explicit_tags_list = c.get("explicit_tags") or c.get("canonical_tags") or []
            for et in explicit_tags_list:
                et_str = str(et).strip()
                if et_str and et_str not in canonical_tags:
                    canonical_tags.append(et_str)
        entry = {
            "identity_key": identity_key,
            "character_key": str(c.get("character_key") or c.get("key") or ""),
            "name_zh": str(c.get("name_zh") or ""),
            "name_en": str(c.get("name_en") or ""),
            "franchise_zh": str(c.get("franchise_zh") or ""),
            "franchise_en": str(c.get("franchise_en") or ""),
            "canonical_tags": canonical_tags,
            "gender": str(c.get("gender") or "female"),
            "source": source,
        }
        result.append(entry)
    return _json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _detect_explicit_user_characters(user_msg: str) -> list[dict[str, Any]]:
    """从用户消息中检测明确指定但不在库内的人物。

    检测三种模式：
    - Booru-style identity tags: yukikaze_(azur_lane), sirius_(azur_lane)_(cosplay)
    - Foreign names (no CJK, looks like a character name): Alfania, Vaporeon
    - CJK names that are not in the library will be handled separately via translation.

    Returns list of explicit_user_character dicts.
    """
    import re as _re
    raw = str(user_msg or "").strip()
    if not raw:
        return []

    result: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # ── 1. Booru-style identity tags ──
    # 匹配: word_word_(franchise) 或 word_(franchise)
    # 如 yukikaze_(azur_lane), shimakaze_(azur_lane), sirius_(azur_lane)_(cosplay)
    booru_pattern = _re.compile(
        r'(?<![a-zA-Z0-9_])'
        r'([a-zA-Z][a-zA-Z0-9_]*(?:_[a-zA-Z][a-zA-Z0-9_]*)*'
        r'\s*[(（][^)）]+[)）](?:_\s*[(（][^)）]+[)）])*)',
        _re.IGNORECASE
    )
    for m in booru_pattern.finditer(raw):
        tag = m.group(1).strip()
        if tag and tag.lower() not in seen_names:
            # Skip if it looks like a non-character pattern
            skip_patterns = ('style', 'quality', 'angle', 'view', 'pose',
                           'light', 'resolution', 'masterpiece', 'best')
            if not any(kw in tag.lower() for kw in skip_patterns):
                seen_names.add(tag.lower())
                result.append(_build_explicit_user_character(
                    original_name=tag,
                    explicit_tags=[tag],
                ))

    # ── 2. Pure foreign names (no CJK, not common words) ──
    if not result:
        # Extract potential foreign names: look for non-CJK words that look like names
        # after removing common instruction words
        from .smart_agent.character_search import extract_possible_character_names as _extract_cn
        # 先移除指令词再检测 CJK，避免 "加入人物" 这种指令词
        # 被误判为人物名，导致跳过外文人名检测。
        cleaned_for_cjk = raw
        for kw in ("加入人物", "加入角色", "添加人物", "添加角色", "选择", "生成", "画", "帮我",
                   "给我", "场景", "服装", "背景", "一个", "一张", "的"):
            cleaned_for_cjk = cleaned_for_cjk.replace(kw, " ")
        cjk_candidate = _extract_cn(cleaned_for_cjk)
        if not cjk_candidate:
            # No CJK name found → try extracting foreign name
            # (already cleaned above)
            cleaned = cleaned_for_cjk
            # Find words that look like proper names (capitalized or non-common)
            name_parts = _re.findall(r'[A-Z][a-z]+(?:[ _][A-Z][a-z]+)*', cleaned)
            if name_parts:
                name = " ".join(name_parts).strip()
                if name and len(name) >= 3:
                    tag_name = name.lower().replace(" ", "_")
                    if tag_name not in seen_names:
                        seen_names.add(tag_name)
                        result.append(_build_explicit_user_character(
                            original_name=name,
                            explicit_tags=[tag_name],
                        ))

    return result


def _build_explicit_user_character(
    original_name: str,
    explicit_tags: list[str] | None = None,
    translated_name: str | None = None,
) -> dict[str, Any]:
    """构建库外用户明确指定的人物记录。

    结构：
    {
      "source": "explicit_user_character",
      "identity_key": None,
      "character_key": None,
      "name_en": translated_name or original_name,
      "name_zh": original_name,
      "original_name": original_name,
      "translated_name": translated_name or None,
      "explicit_tags": [...],
      "canonical_tags": [...],
      "tags": comma-separated canonical_tags,
      "character_tag_source": "explicit_user_character",
      "confirmed_by_user": True,
    }
    """
    tags = list(explicit_tags or [original_name])
    return {
        "key": "",
        "character_key": "",
        "name_en": str(translated_name or original_name or ""),
        "name_zh": str(original_name or ""),
        "aliases": "",
        "category_zh": "",
        "category_en": "",
        "tags": ", ".join(tags),
        "character_tag_source": "explicit_user_character",
        "match_stage": "explicit_user",
        "original_character_name": str(original_name or ""),
        "translated_character_name": str(translated_name or "") if translated_name else "",
        "explicit_tags": tags,
        "canonical_tags": tags,
        "source": "explicit_user_character",
        "identity_key": "",
        "confirmed_by_user": True,
    }


def _resolve_character_operation(
    user_msg: str,
    current_characters_json: str | None,
    found_characters: list[dict[str, Any]],
) -> str:
    """根据用户消息和当前状态，确定消息操作类型。

    Returns:
        "add_characters"      — 添加新人物
        "replace_characters"  — 替换当前人物
        "remove_characters"   — 删除人物
        "query_characters"    — 只读查询（问是谁/有什么区别/当前选了谁）
        "generation_supplement" — 已有选择人物，补充场景/服装/动作等（不修改人物）
        "ordinary_chat"       — 普通聊天，无人物的自由描述
        "generate_confirm"    — 确认生成（prompt_ready 确认）
    """
    raw = (user_msg or "").strip()
    lowered = raw.lower()
    has_current = bool(current_characters_json)
    has_found = bool(found_characters)

    # ── 1. 明确的人物查询（含明确问题标记） ──
    query_patterns = (
        "?", "？", "是什么", "谁", "which one", "what is",
        "有什么区别", "分别是什么", "compare", "difference between",
        "介绍", "说明", "describe",
        "有哪些角色", "有哪些人物", "现在有哪些", "当前人物",
        "当前选了谁", "选了谁", "现在是谁", "现在的人物",
        "list characters", "who is", "current character",
    )
    action_markers = (
        "加入", "添加", "加上", "换成", "改为", "删除", "去掉", "移除",
        "add", "replace", "remove", "switch", "change to",
        "穿", "服装", "场景", "动作", "表情", "构图", "光线", "氛围",
        "改成", "换成", "不要", "换成", "再", "改", "调", "调整",
    )
    is_query = _contains_any(raw, query_patterns) or _contains_any(lowered, query_patterns)
    # 如果同时有动作词 → 不是纯查询，是补充/修改
    has_action = _contains_any(raw, action_markers) or _contains_any(lowered, action_markers)
    if is_query and not has_action:
        return "query_characters"

    # ── 2. 删除人物 ──
    remove_markers = ("删除", "去掉", "移除", "不要", "去除",
                      "remove", "delete", "drop")
    if _contains_any(lowered, remove_markers) and has_found:
        return "remove_characters"

    # ── 3. 替换人物 ──
    replace_markers = ("换成", "改为", "换为", "换成角色", "改成", "改选", "改成角色",
                       "switch to", "change to", "replace with", "换成人物")
    if _contains_any(raw, replace_markers) and has_found:
        return "replace_characters"

    # ── 4. 添加人物 ──
    add_markers = ("加入", "添加", "加上", "追加",
                   "add", "also add", "include")
    if _contains_any(lowered, add_markers) and has_found:
        return "add_characters"

    # ── 5. 无当前人物，有新匹配人物 → add ──
    if not has_current and has_found:
        return "add_characters"

    # ── 6. 有当前人物，有新匹配人物但没有明确操作词 → replace ──
    if has_current and has_found:
        return "replace_characters"

    # ── 7. 已有选择人物，消息包含场景/服装/动作等补充内容 → generation_supplement ──
    if has_current:
        supplement_markers = (
            # 场景
            "场景", "背景", "环境", "地点", "地方", "位置",
            "教室", "学校", "车站", "卧室", "客厅", "公园", "海边", "沙滩",
            "雨天", "晴天", "夜晚", "白天", "黄昏", "傍晚", "早上",
            "室内", "室外", "户外", "街道", "城市",
            # 服装
            "服装", "衣服", "穿着", "穿", "打扮", "装束",
            "校服", "制服", "连衣裙", "裙子", "衬衫", "外套", "毛衣",
            "泳装", "睡衣", "和服", "浴衣",
            # 动作/姿势
            "动作", "姿势", "姿态", "pose",
            "站着", "坐着", "跑", "走", "躺", "蹲", "跳",
            "等", "等人", "等待", "看", "望",
            # 表情
            "表情", "笑容", "微笑", "哭", "生气", "害羞", "失落", "难过", "开心",
            "眼神", "视线", "目光",
            # 构图/镜头
            "构图", "镜头", "视角", "角度", "正面", "侧面", "背面",
            "全身", "半身", "特写", "close",
            # 光线/氛围
            "光线", "光", "灯光", "阳光", "暗", "亮",
            "氛围", "气氛", "感觉",
            # 风格
            "风格", "画风", "画质",
            # 修改
            "改", "换成", "改成", "不要", "调整", "修改", "变成", "变为",
            "再", "另外", "还", "还有", "也要", "也",
        )
        if _contains_any(raw, supplement_markers) or _contains_any(lowered, supplement_markers):
            return "generation_supplement"

    # ── 8. 有当前人物但没有明显补充标记 → 也可能是场景补充（保守判断） ──
    #     比如 "穿校服" 这种简短消息
    if has_current and len(raw) >= 3:
        # 如果消息看起来不像查询，且已有选择的人物 → generation_supplement
        looks_like_chat = any(kw in lowered for kw in ("你好", "谢谢", "在吗", "help", "hi", "hello"))
        if not looks_like_chat:
            return "generation_supplement"

    # ── 9. 普通聊天 ──
    return "ordinary_chat"


def _apply_character_operation(
    operation: str,
    current_json: str | None,
    found_characters: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """根据操作类型更新人物集合。返回 (new_json, resolved_characters)。
    
    支持操作: add_characters, replace_characters, remove_characters,
              query_characters, generation_supplement, ordinary_chat
    """
    import json as _json

    current = []
    if current_json:
        try:
            current = _json.loads(current_json)
        except Exception:
            current = []

    # ── 人物不变的只读/补充操作 ──
    if operation in ("query_characters", "generation_supplement", "ordinary_chat"):
        resolved = _json_to_characters(current_json)
        return (current_json or "[]", resolved)

    if operation in ("query", "replace", "add", "remove"):
        # 兼容旧操作名
        op = operation
        if op == "query":
            return (current_json or "[]", _json_to_characters(current_json))
        elif op == "replace":
            return (_build_selected_characters_json(found_characters), found_characters)
        elif op == "add":
            existing_keys = {c.get("identity_key", "") for c in current}
            new_current = list(current)
            for fc in found_characters:
                identity_key = str(fc.get("character_key") or fc.get("key") or "")
                if identity_key not in existing_keys:
                    new_current.append(fc)
                    existing_keys.add(identity_key)
            new_current = new_current[:3]
            return (_build_selected_characters_json(new_current), new_current)
        elif op == "remove":
            remove_names = set()
            for fc in found_characters:
                remove_names.add(str(fc.get("character_key") or fc.get("key") or ""))
                remove_names.add(str(fc.get("name_zh") or "").lower())
                remove_names.add(str(fc.get("name_en") or "").lower())
            new_current = [c for c in current
                           if c.get("identity_key", "").lower() not in remove_names]
            return (_build_selected_characters_json(new_current),
                    _json_to_characters(_build_selected_characters_json(new_current)))

    if operation == "replace_characters":
        return (_build_selected_characters_json(found_characters), found_characters)

    if operation == "add_characters":
        existing_keys = {c.get("identity_key", "") for c in current}
        new_current = list(current)
        for fc in found_characters:
            identity_key = str(fc.get("character_key") or fc.get("key") or "")
            if identity_key not in existing_keys:
                new_current.append(fc)
                existing_keys.add(identity_key)
        new_current = new_current[:3]
        return (_build_selected_characters_json(new_current), new_current)

    if operation == "remove_characters":
        remove_names = set()
        for fc in found_characters:
            remove_names.add(str(fc.get("character_key") or fc.get("key") or ""))
            remove_names.add(str(fc.get("name_zh") or "").lower())
            remove_names.add(str(fc.get("name_en") or "").lower())
        new_current = [c for c in current
                       if c.get("identity_key", "").lower() not in remove_names]
        return (_build_selected_characters_json(new_current),
                _json_to_characters(_build_selected_characters_json(new_current)))

    return (current_json or "[]", _json_to_characters(current_json))


def _json_to_characters(json_str: str | None) -> list[dict[str, Any]]:
    """将 selected_characters_json 转回字符列表（用于兼容现有代码）。"""
    import json as _json
    if not json_str or json_str == "[]":
        return []
    try:
        data = _json.loads(json_str)
        result = []
        for entry in data:
            if isinstance(entry, dict):
                result.append({
                    "character_key": entry.get("identity_key", entry.get("character_key", "")),
                    "key": entry.get("identity_key", entry.get("character_key", "")),
                    "name_zh": entry.get("name_zh", ""),
                    "name_en": entry.get("name_en", ""),
                    "franchise_zh": entry.get("franchise_zh", ""),
                    "franchise_en": entry.get("franchise_en", ""),
                    "tags": ", ".join(entry.get("canonical_tags", [])),
                    "gender": entry.get("gender", "female"),
                    "character_tag_source": entry.get("source", "character_library"),
                })
        return result
    except Exception:
        return []


def _filter_ds_response_character_tags(
    result: dict[str, Any],
    selected_characters_json: str,
) -> dict[str, Any]:
    """从DS响应中删除所有人物身份tag，只保留非人物tag。

    使用全局身份索引过滤 DS 输出的 translated_prompt, scene, style 等字段。
    但保留 selected_characters_json 中已选人物的 canonical tags。
    """
    from .smart_agent.character_search import build_global_identity_index

    try:
        index = build_global_identity_index()
        foreign_tags = index["all_foreign_tags"]
    except Exception:
        foreign_tags = set()

    # 收集已选人物的受保护 canonical tag keys
    selected_protected_keys: set[str] = set()
    if selected_characters_json:
        resolved = _json_to_characters(selected_characters_json)
        for character in resolved:
            tags_str = str(character.get("tags") or "")
            for tag in _split_prompt_tags(tags_str):
                tag_key = tag.lower().replace(" ", "_").strip("_()")
                if tag_key:
                    selected_protected_keys.add(tag_key)

    def _clean_field(value: str) -> str:
        if not value:
            return value
        tags = _split_prompt_tags(value)
        kept = []
        for tag in tags:
            tag_key = tag.lower().replace(" ", "_").strip("_()")
            # 跳过来自全局索引的身份/作品/分类标签（但保护已选人物 tag）
            if tag_key in foreign_tags and tag_key not in selected_protected_keys:
                continue
            # 跳过看起来像人物标签的（含括号格式），但保护已选人物 tag
            if tag_key not in selected_protected_keys and "(" in tag and ")" in tag and not any(
                kw in tag.lower() for kw in ("style", "quality", "angle", "view", "pose", "light")
            ):
                # 可能是人物身份tag如 yukikaze_(azur_lane)
                continue
            kept.append(tag)
        return ", ".join(kept)

    if not foreign_tags:
        return result

    for key in ("translated_prompt", "draft_prompt", "scene", "style",
                "clothing", "expression", "action", "composition", "mood"):
        value = str(result.get(key) or "")
        if value:
            result[key] = _clean_field(value)

    return result




def _build_local_positive_prompt(
    *,
    result: dict[str, Any],
    request_text: str,
    characters: list[dict[str, Any]],
    snippets: list[dict[str, Any]],
    translated_character_name: str = "",
    character_tag_source: str = "",
    selected_characters_json: str = "",
) -> str:
    """服务器确定性装配最终 Prompt。

    人物 identity tag 只从 selected_characters_json 读取，不从 DS 响应读取。
    装配顺序：人数Tag → selected canonical tags → 清洗后 DS 非人物 tags
    """
    # 先过滤 DS 响应中的人物身份 tag
    result = _filter_ds_response_character_tags(result, selected_characters_json)

    tag_groups: list[str] = []

    # 1. 已确认人物的 canonical tags（从 selected_characters_json，包括库外人物）
    #    这些 tag 永远不会被过滤，始终排在最前面。
    selected_tag_groups: list[str] = []
    resolved = _json_to_characters(selected_characters_json)
    for character in resolved:
        tags_str = str(character.get("tags") or "")
        canonical = _split_prompt_tags(tags_str)
        if canonical:
            selected_tag_groups.append(", ".join(canonical))

    # 2. 非人物 tag：从 DS 响应中收集场景/服饰/动作/构图等（已在上方过滤）
    for key in ("scene", "style", "clothing", "expression", "action", "composition", "mood"):
        value = str(result.get(key) or "")
        if value and not _looks_like_agent_refusal(value):
            tag_groups.append(value)

    # 3. Snippets 和请求中的 tag
    tag_groups.append(", ".join(_tags_from_snippets(snippets, request_text)))
    tag_groups.append(", ".join(_fallback_tags_from_request(request_text)))

    # merger: selected tags first, then DS non-character tags
    merged: list[str] = []
    seen: set[str] = set()

    # 先加入 selected canonical tags（最高优先级，防止被去重挤掉）
    for group in selected_tag_groups:
        for tag in _split_prompt_tags(group):
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)

    # 再加入 DS 非人物 tags
    for group in tag_groups:
        for tag in _split_prompt_tags(group):
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)

    base_prompt = ", ".join(merged[:160])
    base_prompt, _ = _apply_prompt_core_fidelity(
        base_prompt,
        request_text,
        protected_tags=[tag for character in resolved for tag in locked_character_tags(character)],
    )

    # 5. 人数标签注入
    return _apply_count_tags(base_prompt, resolved if resolved else characters)


def _emit_disambiguation_event(
    s: Settings,
    *,
    conversation_id: int,
    disambiguation: dict[str, Any],
    original_request: str,
) -> None:
    """发送人物歧义确认事件并保存状态到数据库。"""
    term = str(disambiguation.get("term") or "")
    candidates = disambiguation.get("candidates", [])
    # 构建提示消息（仅用于存入事件，不单独存 assistant_message）
    lines = [f'检测到"{term}"对应多个角色，请选择：', ""]
    for i, c in enumerate(candidates, 1):
        name_zh = str(c.get("name_zh") or "")
        name_en = str(c.get("name_en") or "")
        franchise = str(c.get("franchise_en") or c.get("franchise_zh") or "")
        lines.append(f"{i}. {name_zh} / {name_en}")
        if franchise:
            lines.append(f"   《{franchise}》")
    lines.append("")
    lines.append("请点击选择或回复序号、角色全名。")
    # 保存歧义状态到数据库(完整数据)
    import json as _json
    save_pending_disambiguation(
        s,
        conversation_id=conversation_id,
        term=term,
        candidates=_json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
        original_request=original_request,
        constraints=original_request,
    )
    # 公开候选列表:白名单过滤,只返回展示所需字段
    public_candidates = []
    for c in candidates:
        # 作品显示：优先 franchise_zh > franchise_en
        franchise_display = str(c.get("franchise_zh") or c.get("franchise_en") or "")
        name_zh = str(c.get("name_zh") or "")
        name_en = str(c.get("name_en") or "")
        # 验证 franchise 不是人物名（防止显示 nanami_mami 等 identity key 作为作品名）
        if (not franchise_display
                or franchise_display.lower().replace("_", " ") in (name_en.lower(), name_zh.lower())):
            franchise_display = str(c.get("franchise_en") or "")
        public_candidates.append({
            "character_key": str(c.get("character_key") or c.get("key") or ""),
            "display_name": name_zh or name_en,
            "display_name_en": name_en,
            "franchise": franchise_display,
        })
    _add_safe_smart_agent_event(
        s,
        conversation_id=conversation_id,
        event_type="character_disambiguation",
        public_message=_json.dumps(
            {
                "term": term,
                "candidates": public_candidates,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")

def _emit_disambiguation_event_v2(
    s,
    *,
    conversation_id,
    pending,
):
    import json as _json
    public_groups = pending_to_public(pending)
    if not public_groups:
        return
    for g_info in public_groups:
        _add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="character_disambiguation",
            public_message=_json.dumps(
                {
                    "group_id": g_info.get("group_id", ""),
                    "term": g_info.get("mention", ""),
                    "candidates": g_info.get("candidates", []),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")


def _reemit_disambiguation_cards(
    s,
    conversation_id,
    pending,
):
    _emit_disambiguation_event_v2(s, conversation_id=conversation_id, pending=pending)




def _save_character_draft(
    s: Settings,
    *,
    conversation_id: int,
    selected_characters_json: str,
    resolved_intent: str = "chat",
) -> None:
    """保存仅有人物的草稿（status=drafting，不触发 prompt_ready）。
    
    当 add_characters / remove_characters / replace_characters 操作完成
    但不进入生成路径时调用，确保后续轮次的 generation_supplement
    能读取到已选人物。
    """
    import json as _json, time as _time
    now = int(_time.time())
    conn = connect(s)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        next_version = int(existing["prompt_version"] or 0) + 1 if existing else 1
        previous_plan: dict[str, Any] = {}
        previous_request = ""
        previous_structured = ""
        previous_width = 832
        previous_height = 1216
        if existing:
            previous_request = str(existing["request_text"] or "")
            previous_structured = str(existing["structured_draft_json"] or "")
            previous_width = int(existing["width"] or previous_width)
            previous_height = int(existing["height"] or previous_height)
            try:
                parsed_plan = _json.loads(str(existing["plan_json"] or "{}"))
                if isinstance(parsed_plan, dict):
                    previous_plan = parsed_plan
            except Exception:
                previous_plan = {}
        previous_plan["selected_characters_json"] = selected_characters_json
        previous_plan["resolved_intent"] = resolved_intent
        if previous_request:
            previous_plan.setdefault("previous_request_text", previous_request)
        if previous_structured:
            previous_plan.setdefault("previous_structured_draft_json", previous_structured)
        plan = _json.dumps(previous_plan, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            """INSERT INTO smart_agent_prompt_drafts(
                conversation_id, prompt_draft, prompt_version, status, ready_at,
                plan_json, request_text, workflow_key, loras_json, prompt_source,
                fallback_level, width, height, structured_draft_json, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                prompt_draft=excluded.prompt_draft,
                prompt_version=excluded.prompt_version,
                status=excluded.status,
                plan_json=excluded.plan_json,
                request_text=excluded.request_text,
                fallback_level=excluded.fallback_level,
                width=excluded.width,
                height=excluded.height,
                structured_draft_json=excluded.structured_draft_json,
                updated_at=excluded.updated_at
            """,
            (
                int(conversation_id),
                "[character_selected]",
                next_version,
                "drafting",
                now,
                plan,
                previous_request or resolved_intent,
                "",
                "[]",
                "character_operation",
                "character_only",
                previous_width, previous_height,
                previous_structured,
                now, now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _process_smart_agent_chat_message_legacy(
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
    request_id = hashlib.sha256(f"{conversation_code}:{client_request_id or ''}:{time.time()}".encode("utf-8")).hexdigest()[:12]
    message_terminal = False
    try:
        if message_id:
            mark_smart_agent_message_status(s, message_id=message_id, status="processing")
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="message_processing",
                public_message="正在处理上一条消息......",
            )
        _smart_trace("smart_message_received", request_id=request_id, conversation_code=conversation_code)
        conv = get_conversation_by_code(s, conversation_code=conversation_code)
        if not conv:
            return

        recent = get_conversation_messages(s, conversation_id=conversation_id, limit=20)
        memory = str(conv.get("memory_summary") or "")

        # ── 歧义处理:使用新版歧义引擎 ──
        pending_dis = get_pending_disambiguation_json(s, conversation_id=conversation_id)

        if pending_dis:
            # 已有未解决的歧义
            _smart_trace("disambiguation_pending_v2", request_id=request_id, conversation_code=conversation_code)

            # A. 用户发送新的完整生成请求 → supersede 旧 pending
            if is_new_generation_request(user_msg):
                _smart_trace("disambiguation_superseded", request_id=request_id, conversation_code=conversation_code)
                supersede_pending_disambiguation(s, conversation_id=conversation_id)
                # 继续处理新请求（不走歧义逻辑）
                pending_dis = None

            # B. 用户选择 → 解析
            elif pending_dis:
                # 收集所有候选
                all_candidates = []
                for g in pending_dis.get("groups", []):
                    if g.get("status") == "pending":
                        all_candidates.extend(g.get("candidates", []))

                if is_disambiguation_choice(user_msg, all_candidates):
                    # 尝试为每个 group 解析用户选择
                    resolved_count = 0
                    for g in pending_dis.get("groups", []):
                        if g.get("status") == "resolved":
                            resolved_count += 1
                            continue
                        candidates = g.get("candidates", [])
                        matched = resolve_character_from_candidates(candidates, user_msg)
                        if matched:
                            ik = str(matched.get("identity_key") or matched.get("character_key") or "")
                            resolve_group(pending_dis, g["group_id"], ik)
                            resolved_count += 1
                            char_name = str(matched.get("name_zh") or matched.get("name_en") or "")
                            _smart_trace("disambiguation_resolved_v2", request_id=request_id,
                                        group_id=g["group_id"], character_key=ik)

                    if resolved_count > 0:
                        save_pending_disambiguation_json(s, conversation_id=conversation_id, disambiguation_json=pending_dis)

                        # 检查是否全部解决
                        if all_groups_resolved(pending_dis):
                            # 组装已选角色继续处理
                            all_selected = []
                            for g in pending_dis.get("groups", []):
                                for c in g.get("candidates", []):
                                    if c.get("identity_key") == g.get("selected_identity_key"):
                                        all_selected.append(c)
                            resolved_char_names = [c.get("name_zh", c.get("name_en", "")) for c in all_selected]
                            char_display = "、".join(resolved_char_names) if resolved_char_names else "已选择"

                            confirm_msg = f"已确认为{char_display}，保留你之前的要求。正在继续整理生成方案……"
                            add_conversation_message(s, conversation_id=conversation_id, role="assistant",
                                                     content=confirm_msg, safe_content=confirm_msg)
                            _add_safe_smart_agent_event(s, conversation_id=conversation_id,
                                                        event_type="assistant_message", public_message=confirm_msg)

                            # 将确认信息合并到用户请求
                            original_request = str(pending_dis.get("original_request") or "")
                            combined = f"{char_display},{original_request}"
                            user_msg = combined
                            memory = f"用户已确认角色:{char_display}。{original_request}"

                            # 清除 pending 并继续处理
                            clear_pending_disambiguation(s, conversation_id=conversation_id)
                            # 不 return，继续正常流程
                        else:
                            # 还有未解决的 group，重新显示卡片
                            _reemit_disambiguation_cards(s, conversation_id, pending_dis)
                            if message_id:
                                mark_smart_agent_message_status(s, message_id=message_id, status="done")
                            return
                    else:
                        # 无法解析，重新显示候选
                        _smart_trace("disambiguation_parse_failed_v2", request_id=request_id)
                        _reemit_disambiguation_cards(s, conversation_id, pending_dis)
                        if message_id:
                            mark_smart_agent_message_status(s, message_id=message_id, status="done")
                        return

                # C. 场景补充 → 保存到 constraints
                elif is_scene_supplement(user_msg):
                    constraints = pending_dis.get("constraints", {})
                    supplements = constraints.get("supplements", [])
                    supplements.append(user_msg)
                    constraints["supplements"] = supplements[-5:]  # 最多保留5条
                    pending_dis["constraints"] = constraints
                    save_pending_disambiguation_json(s, conversation_id=conversation_id, disambiguation_json=pending_dis)

                    confirm_msg = f"已记录补充信息：{user_msg}。请继续选择角色～"
                    add_conversation_message(s, conversation_id=conversation_id, role="assistant",
                                             content=confirm_msg, safe_content=confirm_msg)
                    _add_safe_smart_agent_event(s, conversation_id=conversation_id,
                                                event_type="assistant_message", public_message=confirm_msg)
                    _add_safe_smart_agent_event(s, conversation_id=conversation_id,
                                                event_type="done", public_message="")
                    if message_id:
                        mark_smart_agent_message_status(s, message_id=message_id, status="done")
                    return

                # D. 其他消息 → 重新显示候选
                else:
                    _reemit_disambiguation_cards(s, conversation_id, pending_dis)
                    if message_id:
                        mark_smart_agent_message_status(s, message_id=message_id, status="done")
                    return

        should_create_task = resolved_intent in {"generate", "regenerate", "edit"}
        character_query_text = user_msg
        if should_create_task:
            recent_visual_context = "\n".join(
                str(message.get("content") or message.get("safe_content") or "")
                for message in recent[-10:]
                if message.get("role") in {"user", "assistant"}
            )
            character_query_text = "\n".join(part for part in (user_msg, memory, recent_visual_context) if str(part or "").strip())
        # 优先从当前用户消息匹配人物，避免对话历史中的人物干扰
        characters = _dedupe_character_matches(find_characters(user_msg))
        if not characters and character_query_text != user_msg:
            characters = _dedupe_character_matches(find_characters(character_query_text))

        # ── 新版歧义检测:使用全库索引分析用户请求 ──
        if not pending_dis:
            analysis = analyze_user_request(user_msg)
            if analysis.get("is_ambiguous") and should_create_task:
                _smart_trace("character_ambiguous_v2", request_id=request_id, conversation_code=conversation_code,
                           ambiguous_count=analysis.get("ambiguous_count"))
                pending = create_pending_disambiguation_json(analysis, user_msg)
                save_pending_disambiguation_json(s, conversation_id=conversation_id, disambiguation_json=pending)
                _emit_disambiguation_event_v2(s, conversation_id=conversation_id, pending=pending)
                if message_id:
                    mark_smart_agent_message_status(s, message_id=message_id, status="done")
                return

        # 保留 characters 列表用于后续处理（从 resolved_characters 构建）
        characters = []
        if not pending_dis:
            # 优先从当前用户消息匹配，避免对话历史干扰
            chars_raw = _dedupe_character_matches(find_characters(user_msg))
            analysis = analyze_user_request(user_msg)
            if not chars_raw and character_query_text != user_msg:
                chars_raw = _dedupe_character_matches(find_characters(character_query_text))
                analysis = analyze_user_request(character_query_text)
            resolved_iks = {c.get("identity_key") for c in analysis.get("resolved_characters", [])}
            # resolved + 没有歧义的匹配
            for c in chars_raw:
                ik = _canonical_name_key(str(c.get("key") or c.get("name_en") or "")).replace(" ", "_")
                if ik in resolved_iks:
                    characters.append(c)
            # 如果只有1个字符且无歧义，直接使用
            if not characters and len(chars_raw) == 1 and not analysis.get("is_ambiguous"):
                characters = chars_raw
        else:
            # pending_dis 已解决，构建 characters
            for g in pending_dis.get("groups", []):
                if g.get("status") == "resolved":
                    for c in g.get("candidates", []):
                        if c.get("identity_key") == g.get("selected_identity_key"):
                            characters.append({
                                "key": c.get("character_key", ""),
                                "name_zh": c.get("name_zh", ""),
                                "name_en": c.get("name_en", ""),
                                "aliases": "",
                                "category_zh": c.get("franchise_zh", ""),
                                "category_en": c.get("franchise_en", ""),
                                "tags": "",
                            })

        translated_character_name = ""
        original_character_name = ""
        character_tag_source = ""  # 必须在此初始化，避免 UnboundLocalError
        if not characters:
            # ── 优先检测库外明确人物（Booru Tag / 外文人名等）──
            # extract_possible_character_names 只提取 CJK，导致非 CJK 的
            # Booru Tag（如 yukikaze_(azur_lane)）被完全忽略。
            # 先行检测 explicit_user_characters，避免走到翻译路径丢失。
            explicit_chars = _detect_explicit_user_characters(user_msg)
            if explicit_chars:
                characters = explicit_chars
                character_tag_source = "explicit_user_character"
                _smart_trace(
                    "character_explicit_user_detected",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    count=len(explicit_chars),
                    names=[c.get("original_character_name", "") for c in explicit_chars],
                )
            else:
                # 原有的 CJK 翻译 + 二次匹配链路
                candidate_cn = extract_possible_character_names(character_query_text)
                if candidate_cn:
                    original_character_name = candidate_cn
                    # 先检查原始 CJK 文本是否能直接匹配库内人物
                    direct_cn_matches = _dedupe_character_matches(find_characters(candidate_cn))
                    translated = await translate_character_name(candidate_cn)
                    if translated and translated != candidate_cn:
                        translated_character_name = translated
                        translated_matches = _dedupe_character_matches(find_characters(translated))
                        if translated_matches:
                            # 如果原始 CJK 文本在库内无匹配，但翻译后匹配到了
                            # → 可能是翻译歧义（用户的名字恰好翻译成库内另一个角色）
                            # 安全策略：只有原始 CJK 能直接匹配时才用库内角色，
                            # 否则创建 explicit_user_character 保护用户输入。
                            if direct_cn_matches:
                                characters = translated_matches
                                for item in characters:
                                    item["character_tag_source"] = "character_registry"
                                    item["match_stage"] = "translated"
                            else:
                                # 翻译后匹配到库内角色，但原始 CJK 无匹配
                                # → 创建 explicit_user_character 而不冒用库内角色
                                characters = [_build_explicit_user_character(
                                    original_name=original_character_name,
                                    translated_name=translated_character_name,
                                    explicit_tags=[translated_character_name.strip().lower().replace(" ", "_")],
                                )]
                                character_tag_source = "explicit_user_character"
                            if characters:
                                _smart_trace(
                                    "character_matched_after_translation",
                                    request_id=request_id,
                                    conversation_code=conversation_code,
                                    character_key=stable_character_key(characters[0]),
                                    translated_name=translated_character_name,
                                )
        character_tag_source = character_tag_source or ""
        if not characters and (translated_character_name or original_character_name):
            # 翻译成功但找不到匹配 → 作为显式人物保存，避免丢失
            # 翻译失败时(返回原名)，也用原始名创建 fallback，保证链路不断
            effective_name = translated_character_name or original_character_name
            fallback = build_agent_fallback_character(effective_name, original_character_name)
            if fallback:
                # 标记为 explicit_user_character 而非 agent_fallback，
                # 确保后续过滤不会把用户明确指定的人物删除。
                fallback["character_tag_source"] = "explicit_user_character"
                fallback["source"] = "explicit_user_character"
                fallback["explicit_tags"] = [
                    effective_name.strip().lower().replace(" ", "_")
                ] if effective_name else []
                fallback["canonical_tags"] = list(fallback["explicit_tags"])
                characters = [fallback]
                character_tag_source = "explicit_user_character"
                _smart_trace(
                    "character_fallback_explicit_user",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    translated_name=effective_name,
                )
                if not translated_character_name and original_character_name:
                    print(f"[SMART_AGENT] translator_unavailable_using_original name={original_character_name}", flush=True)
        character_key = stable_character_key(characters[0]) if characters else ""
        _smart_trace("character_matched", request_id=request_id, conversation_code=conversation_code, character_key=character_key)

        # ── 四种人物操作：服务器本地确定 ──
        current_draft = await asyncio.to_thread(get_smart_agent_prompt_draft, s, conversation_id=conversation_id)
        draft_context_text = _smart_agent_draft_context_text(current_draft)
        prompt_text_requested = _is_prompt_text_request(user_msg)
        current_selected_json = ""
        if current_draft and current_draft.get("plan_json"):
            try:
                plan = json.loads(str(current_draft.get("plan_json") or "{}"))
                current_selected_json = str(plan.get("selected_characters_json") or "")
            except Exception:
                pass

        char_operation = _resolve_character_operation(user_msg, current_selected_json, characters)
        selected_characters_json, characters = _apply_character_operation(
            char_operation, current_selected_json, characters,
        )
        _smart_trace(
            "character_operation",
            request_id=request_id,
            conversation_code=conversation_code,
            operation=char_operation,
            selected_count=len(_json_to_characters(selected_characters_json)),
        )
        should_plan_prompt = should_create_task or prompt_text_requested
        prompt_request_text = "\n".join(
            part for part in (user_msg, draft_context_text)
            if should_plan_prompt and str(part or "").strip()
        ) or user_msg
        generation_memory_context = "\n".join(
            part for part in (memory, draft_context_text)
            if str(part or "").strip()
        )

        # 查询操作：只读回答，不修改草稿、不调用生成 Planner
        if char_operation in ("query_characters", "query"):
            resolved = _json_to_characters(selected_characters_json)
            if resolved:
                names = []
                for rc in resolved:
                    zh = rc.get("name_zh", "")
                    en = rc.get("name_en", "")
                    fr = rc.get("franchise_zh", "")
                    if fr:
                        names.append(f"{zh}(《{fr}》)" if zh else en)
                    else:
                        names.append(zh or en)
                query_reply = f"当前人物：{'、'.join(names)}"
            else:
                query_reply = "当前还没有选择人物。"

            # 回答人物查询
            if characters and char_operation in ("query_characters", "query"):
                found_names = [f"{c.get('name_zh','')}({c.get('franchise_zh','')})" for c in characters]
                query_reply = "\n".join(found_names)

            add_conversation_message(s, conversation_id=conversation_id, role="assistant",
                                     content=query_reply, safe_content=query_reply)
            _add_safe_smart_agent_event(s, conversation_id=conversation_id,
                                        event_type="assistant_message", public_message=query_reply)
            _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")
            if message_id:
                mark_smart_agent_message_status(s, message_id=message_id, status="done")
            return

        char_text = "\n".join(
            f"- {c['name_zh']} / {c['name_en']}: {c['tags']}" for c in characters
        ) or "- none"
        snippets = search_prompt_snippets(prompt_request_text)
        snippet_text = snippets_for_prompt(snippets)
        try:
            _validate_request_policy(s, user_msg)
        except SmartAgentError as exc:
            _smart_trace("local_policy_blocked", request_id=request_id, conversation_code=conversation_code, error_code=exc.code)
            safe_message = sanitize_public_agent_message(str(exc) or "Smart Agent 目前只支持安全的图片生成请求。")
            add_conversation_message(s, conversation_id=conversation_id, role="assistant", content=safe_message, safe_content=safe_message)
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="failed",
                public_message=safe_message,
                private_detail=exc.code,
            )
            return

        _smart_trace(
            "resolved_intent",
            request_id=request_id,
            conversation_code=conversation_code,
            character_key=character_key,
            resolved_intent=resolved_intent,
            should_create_task=should_create_task,
        )

        scene_delegated = _is_scene_delegation_request(user_msg)
        agent_user_msg = user_msg
        if should_plan_prompt and draft_context_text:
            agent_user_msg = (
                f"{user_msg}\n\n"
                "当前会话已有草案/上一版方案信息如下。"
                "如果用户只说开始生成、按刚才方案生成、其他不变，请基于这些信息整理新的生图方案；"
                "如果用户刚刚替换/添加了人物，请保留原场景、服装、动作、表情和构图，只替换人物。\n"
                f"{draft_context_text}"
            )
        if prompt_text_requested:
            agent_user_msg = (
                f"{agent_user_msg}\n\n"
                "(用户这次只想查看英文生图 Prompt，不要声称任务已创建，不要提示已提交。"
                "请整理可用于生图的非人物视觉字段；后端会把最终 Prompt 作为文本回复。)"
            )
        if scene_delegated:
            agent_user_msg = f"{agent_user_msg}\n\n(用户已授权我选择场景,我可以选一个合理场景。)"
            _smart_trace("scene_delegated", request_id=request_id, conversation_code=conversation_code, character_key=character_key)

        # ── Agent 调用审计日志 ──
        agent_call_count = 0
        agent_model_name = str(getattr(s, "deepseek_model", "") or "deepseek")
        _smart_trace(
            "agent_call_started",
            request_id=request_id,
            conversation_code=conversation_code,
            agent_model=agent_model_name,
            agent_call_count=0,
        )

        try:
            _smart_trace("chat_structuring_started", request_id=request_id, conversation_code=conversation_code, character_key=character_key)
            result = await chat_with_agent(
                s,
                user_message=agent_user_msg,
                memory_summary=memory,
                recent_messages=[
                    {"role": str(m["role"]), "content": str(m.get("content") or m.get("safe_content") or "")}
                    for m in recent
                    if m.get("role") in {"user", "assistant"}
                ],
                workflow_summary=workflow_summaries(),
                lora_summary=lora_summaries(),
                matched_characters=char_text,
                snippet_summary=snippet_text,
            )
            agent_call_count = 1
            meaningful_tags = _count_meaningful_non_character_tags(result=result, snippets=snippets)
            _smart_trace(
                "agent_call_finished",
                request_id=request_id,
                conversation_code=conversation_code,
                agent_model=agent_model_name,
                agent_call_count=agent_call_count,
                meaningful_tag_count=meaningful_tags,
                finish_reason=str(result.get("_finish_reason") or ""),
            )
            _smart_trace(
                "chat_structuring_completed",
                request_id=request_id,
                conversation_code=conversation_code,
                finish_reason=str(result.get("_finish_reason") or ""),
            )
        except Exception as exc:
            _smart_trace("agent_call_failed", request_id=request_id, conversation_code=conversation_code,
                         agent_model=agent_model_name, error_code=type(exc).__name__[:80],
                         error_type="deepseek_api")
            _smart_trace("agent_planning_failed", request_id=request_id, conversation_code=conversation_code, error_code=type(exc).__name__[:80])
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="error",
                public_message="智能 Agent 暂时无法连接，请稍后重试。",
                private_detail=f"agent_unavailable: {type(exc).__name__[:100]}",
            )
            if message_id:
                mark_smart_agent_message_status(s, message_id=message_id, status="done")
            return

        if not characters and character_tag_source != "explicit_user_character":
            # 用 DS 响应字段做二次匹配（仅对非 explicit_user 人物）
            # explicit_user_character 不需要 DS 帮忙找人，用户已经指定了。
            translated_character_text = " ".join(
                str(result.get(key) or "")
                for key in (
                    "draft_prompt",
                    "scene",
                    "style",
                    "clothing",
                    "expression",
                    "action",
                    "composition",
                    "mood",
                )
            )
            candidates = result.get("character_candidates") or []
            if isinstance(candidates, list):
                translated_character_text = " ".join([translated_character_text, *[str(item) for item in candidates]])
            characters = _dedupe_character_matches(
                find_character_after_translation(character_query_text, translated_character_text)
            )
            if characters:
                character_tag_source = "character_registry"
                for item in characters:
                    item["character_tag_source"] = "character_registry"
                    item.setdefault("match_stage", "translated")
                _smart_trace(
                    "character_matched_after_translation",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    character_key=stable_character_key(characters[0]),
                )
            elif translated_character_name or original_character_name:
                # 二次匹配仍失败,回退到 agent_fallback
                # 翻译失败时也用原始名创建 fallback
                effective_name = translated_character_name or original_character_name
                fallback = build_agent_fallback_character(effective_name, original_character_name)
                if fallback:
                    characters = [fallback]
                    character_tag_source = "agent_fallback"
                    _smart_trace(
                        "character_fallback_agent",
                        request_id=request_id,
                        conversation_code=conversation_code,
                        translated_name=effective_name,
                    )
            character_key = stable_character_key(characters[0]) if characters else ""

        # 当人物为 agent_fallback(未真实匹配)时,剥离 DS 响应的 hallucinated umamusume 标签
        if character_tag_source == "agent_fallback" and characters:
            for key in ("draft_prompt", "scene", "style", "clothing", "expression", "action", "composition", "mood"):
                value = str(result.get(key) or "")
                if value:
                    result[key] = strip_umamusume_identity_tags(value)

        reply = sanitize_public_agent_message(str(result.get("reply") or ""))
        reply_emitted = False
        if reply and not prompt_text_requested:
            if _looks_like_task_execution_claim(reply):
                if should_create_task:
                    reply = "我正在为你整理生成方案。"
                else:
                    reply = "我会继续帮你整理方案,确认生成时再提交任务。"
            add_conversation_message(s, conversation_id=conversation_id, role="assistant", content=reply, safe_content=reply)
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=reply,
            )
            reply_emitted = True

        memory_update = sanitize_public_agent_message(str(result.get("memory_update") or ""))
        if memory_update:
            update_conversation_summary(s, conversation_id=conversation_id, memory_summary=memory_update)

        if not should_create_task:
            # Even for chat intent, DS may have returned a usable plan - check and redirect.
            # BUT: 纯人物操作（add/remove/replace）不应该翻转为生成，即使 DS 返回了数据。
            # 只有用户明确要求生成或有非人物补充时才算生成就绪。
            char_ops_that_stay_chat = (
                not prompt_text_requested and
                char_operation in ("add_characters", "remove_characters", "replace_characters", "add", "remove", "replace")
            )
            if char_ops_that_stay_chat:
                _smart_trace(
                    "agent_skipped_generation",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    agent_skip_reason=f"character_operation_{char_operation}",
                    character_operation=char_operation,
                )
                has_plan_data = False
            else:
                has_plan_data = _result_has_generation_details(
                    result=result,
                    characters=characters,
                    snippets=snippets,
                    memory=generation_memory_context,
                    recent_messages=recent,
                )
            meaningful_count = _count_meaningful_non_character_tags(result=result, snippets=snippets)
            gr_reason = "character_only"
            if has_plan_data:
                gr_reason = "meaningful_non_char_tags"
            elif char_ops_that_stay_chat:
                gr_reason = f"character_operation_{char_operation}"
            _smart_trace(
                "generation_readiness_checked",
                request_id=request_id,
                conversation_code=conversation_code,
                generation_readiness="true" if has_plan_data else "false",
                generation_readiness_reason=gr_reason,
                meaningful_tag_count=meaningful_count,
                character_count=len(characters),
                character_operation=char_operation,
            )
            if has_plan_data:
                _smart_trace(
                    "chat_intent_plan_detected",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    character_key=character_key,
                )
                should_create_task = True  # fall through to generate path below
            else:
                if not reply_emitted:
                    for step in result.get("public_steps") or []:
                        _add_safe_smart_agent_event(
                            s,
                            conversation_id=conversation_id,
                            event_type="thinking",
                            public_message=str(step),
                        )
                        await asyncio.sleep(0.25)
                # 有人物但无场景 → 主动询问场景/服装/动作
                local_reply = None
                if prompt_text_requested:
                    local_reply = "暂时没有整理出可用的英文 Prompt，请补充角色、场景、服装或动作后再试。"
                elif characters and not reply:
                    char_names = "、".join(
                        (c.get("name_zh") or c.get("name_en") or "") for c in characters
                    )
                    if char_names:
                        local_reply = f"已加入人物 {char_names}。请继续告诉我场景、服装、动作或画面风格喵～"
                    else:
                        local_reply = "已记录人物。请继续告诉我场景、服装、动作或画面风格喵～"
                elif not reply:
                    local_reply = "我在喵。直接描述想生成的图片、角色或场景,我就可以帮你整理出图。"
                if local_reply:
                    add_conversation_message(s, conversation_id=conversation_id, role="assistant", content=local_reply, safe_content=local_reply)
                    _add_safe_smart_agent_event(
                        s,
                        conversation_id=conversation_id,
                        event_type="assistant_message",
                        public_message=local_reply,
                    )
                # ── 人物操作后保存草稿（即使不生成）以便后续轮次读取 ──
                if selected_characters_json and selected_characters_json != "[]" and char_operation in (
                    "add_characters", "remove_characters", "replace_characters",
                    "add", "remove", "replace",
                ):
                    _save_character_draft(s, conversation_id=conversation_id,
                                          selected_characters_json=selected_characters_json,
                                          resolved_intent=resolved_intent)
                    _smart_trace("character_draft_saved", request_id=request_id,
                                 conversation_code=conversation_code,
                                 character_operation=char_operation,
                                 selected_count=len(_json_to_characters(selected_characters_json)))
                _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")
                return

        # === Generate/Edit path below ===
        # If DS returned no plan data, retry once with an explicit structured prompt
        if should_create_task and not _result_has_generation_details(
            result=result,
            characters=characters,
            snippets=snippets,
            memory=generation_memory_context,
            recent_messages=recent,
        ):
            _smart_trace("planner_fallback_started", request_id=request_id, conversation_code=conversation_code)
            try:
                fallback_msg = (
                    f"用户需求:{agent_user_msg}\n"
                    f"上下文:{memory}\n\n"
                    "请直接返回一个 JSON 对象,键名:reply, scene, style, clothing, expression, action, composition, mood。"
                    "不要加任何 markdown 标记或额外文字。"
                    "不要输出任何人物身份 tag（如角色名、作品名、franchise tag）。"
                )
                result = await chat_with_agent(
                    s,
                    user_message=fallback_msg,
                    memory_summary=memory,
                    recent_messages=[
                        {"role": str(m["role"]), "content": str(m.get("content") or m.get("safe_content") or "")}
                        for m in recent[-6:]
                        if m.get("role") in {"user", "assistant"}
                    ],
                    workflow_summary=workflow_summaries(),
                    lora_summary=lora_summaries(),
                    matched_characters=char_text,
                    snippet_summary=snippet_text,
                )
                _smart_trace("planner_fallback_completed", request_id=request_id, conversation_code=conversation_code,
                             finish_reason=str(result.get("_finish_reason") or ""))
            except Exception as exc:
                _smart_trace("planner_fallback_failed", request_id=request_id, conversation_code=conversation_code,
                             error_code=type(exc).__name__[:80])
                # Fall through - will use whatever data we have

        if not s.deepseek_api_key:
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="error",
                public_message="Smart Agent 暂未配置,请稍后再试。",
                private_detail="no_api_key",
            )
            return

        if _is_bare_generate_request(user_msg) and not _result_has_generation_details(
            result=result,
            characters=characters,
            snippets=snippets,
            memory=generation_memory_context,
            recent_messages=recent,
        ):
            safe_message = "还没有足够的方案信息,请先告诉我角色、场景或选择哪一个方案。"
            add_conversation_message(s, conversation_id=conversation_id, role="assistant", content=safe_message, safe_content=safe_message)
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=safe_message,
            )
            _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")
            return

        progress_steps = [
            ("searching_character_tags", "正在识别目标人物......"),
            ("searching_prompt_library", "正在匹配合适的提示词片段......"),
            ("selecting_workflow", "正在查找人物专属工作流......"),
            ("selecting_lora", "正在选择合适的生成方案......"),
            ("selecting_resolution", "正在选择合适画幅......"),
            ("building_prompt", "正在整理最终提示词......"),
            ("validating_plan", "正在检查生成方案......"),
        ]
        for event_type, pub_msg in progress_steps:
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type=event_type,
                public_message=pub_msg,
            )
            await asyncio.sleep(0.55)

        workflow_key = SMART_AGENT_DEFAULT_WORKFLOW_KEY
        if not get_workflow(workflow_key):
            workflow_key = SMART_AGENT_DEFAULT_WORKFLOW_KEY
        if not get_workflow(workflow_key):
            _smart_trace("workflow_not_found", request_id=request_id, conversation_code=conversation_code, error_code="workflow_not_found")
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="failed",
                public_message="没有找到可用的生成方案,本次未扣费。",
                private_detail="workflow_not_found",
            )
            return
        resolution_key = _local_resolution_key(result, user_msg)
        resolution = get_resolution_or_default(resolution_key)
        positive_prompt = _build_local_positive_prompt(
            result=result,
            request_text=prompt_request_text,
            characters=characters,
            snippets=snippets,
            translated_character_name=translated_character_name,
            character_tag_source=character_tag_source,
            selected_characters_json=selected_characters_json,
        )[:3000].strip()
        negative_prompt = str(result.get("negative_prompt") or "")[:1600]
        if _looks_like_agent_refusal(negative_prompt):
            negative_prompt = ""
        loras = sanitize_loras([], workflow_key)
        enforced = enforce_character_preferences(
            characters=characters,
            workflow_key=workflow_key,
            positive_prompt=positive_prompt,
            loras=loras,
            is_admin=str(legacy_id) == str(s.owner_user_id),
            request_text=prompt_request_text,
        )
        workflow_key = str(enforced["workflow_key"])
        if workflow_key == "anima_owner":
            workflow_key = SMART_AGENT_DEFAULT_WORKFLOW_KEY
        positive_prompt = str(enforced["positive_prompt"])[:3000].strip()
        protected_tags = [
            tag
            for character in _json_to_characters(selected_characters_json)
            for tag in locked_character_tags(character)
        ]
        positive_prompt, core_fidelity = _apply_prompt_core_fidelity(
            positive_prompt,
            prompt_request_text,
            protected_tags=protected_tags,
        )
        loras = list(enforced["loras"])
        removed_foreign_count = int(enforced.get("foreign_character_tags_removed_count") or 0)
        removed_appearance_count = int(enforced.get("inferred_appearance_tags_removed_count") or 0)
        fallback_level = str(enforced.get("fallback_level") or ("character_tags" if characters else "none"))
        character_workflow_key = str(enforced.get("character_workflow_key") or "")
        workflow_source = fallback_level if fallback_level in {"character_workflow", "character_lora", "character_tags", "type_workflow"} else "smart_agent_default"
        _smart_trace(
            "workflow_selected",
            request_id=request_id,
            conversation_code=conversation_code,
            character_key=character_key,
            workflow_key=workflow_key,
            workflow_source=workflow_source,
            fallback_level=fallback_level,
            internal_lora_count=int(enforced.get("internal_lora_count") or 0),
            external_lora_count=int(enforced.get("external_lora_count") or 0),
            workflow_health=str(enforced.get("workflow_health_status") or "unknown"),
            foreign_character_tags_removed_count=removed_foreign_count,
            inferred_appearance_tags_removed_count=removed_appearance_count,
            core_constraint_added_count=len(core_fidelity.get("added") or []),
            core_conflict_removed_count=len(core_fidelity.get("removed") or []),
        )
        selected_characters = public_character_matches(characters)
        if fallback_level == "character_workflow":
            selection_message = "已找到人物专属工作流,正在应用默认修复配置。"
        elif fallback_level == "type_workflow":
            selection_message = "已找到匹配的类型工作流,正在应用默认配置。"
        elif fallback_level == "character_lora":
            selection_message = "使用通用 workflow,并正在补充角色 LoRA。"
        elif fallback_level == "character_tags":
            selection_message = "使用通用 workflow,并正在补充角色 tag。"
        else:
            selection_message = "使用通用 workflow。"
        _add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="workflow_selected",
            public_message=selection_message,
        )

        if not positive_prompt:
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="failed",
                public_message="Agent 没有生成有效 Prompt,请补充描述后再试。",
            )
            return
        if character_tag_source != "agent_fallback" and character_key:
            try:
                validate_character_prompt(
                    prompt=positive_prompt,
                    character=characters[0] if characters else None,
                    workflow_key=workflow_key,
                    loras=loras,
                    user_text=prompt_request_text,
                )
            except CharacterPromptValidationError:
                _smart_trace(
                    "character_prompt_validation_failed",
                    request_id=request_id,
                    conversation_code=conversation_code,
                    character_key=character_key,
                    workflow_key=workflow_key,
                    foreign_character_tags_removed_count=removed_foreign_count,
                )
                _add_safe_smart_agent_event(
                    s,
                    conversation_id=conversation_id,
                    event_type="failed",
                    public_message="人物提示词整理失败,请重新尝试;如果该人物不在人物库中,系统将使用翻译后的名称继续生成。",
                    private_detail="character_prompt_validation_failed",
                )
                return

        plan_json = json.dumps(
            {
                "workflow_key": workflow_key,
                "resolution_key": resolution_key,
                "width": resolution["width"],
                "height": resolution["height"],
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "loras": loras,
                "fallback_level": fallback_level,
                "workflow_source": workflow_source,
                "character_key": character_key,
                "locked_character_tags": enforced.get("locked_character_tags") or [],
                "foreign_character_tags_removed_count": removed_foreign_count,
                "character_workflow_key": character_workflow_key,
                "allow_external_lora": bool(enforced.get("allow_external_lora")),
                "character_tag_injected": bool(enforced.get("character_tag_injected")),
                "internal_lora_count": int(enforced.get("internal_lora_count") or 0),
                "external_lora_count": int(enforced.get("external_lora_count") or 0),
                "workflow_health_status": str(enforced.get("workflow_health_status") or "unknown"),
                "selected_characters": selected_characters,
                "selected_characters_json": selected_characters_json,
                "conversation_code": conversation_code,
                "conversation_id": conversation_id,
                "character_tag_source": character_tag_source,
                "translated_character_name": translated_character_name,
                "original_character_name": original_character_name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # 清理 Prompt 中与已确认人物无关的外国人物 tag（支持多人）
        resolved_chars = _json_to_characters(selected_characters_json)
        if resolved_chars:
            positive_prompt = _clean_foreign_character_tags_multi(positive_prompt, resolved_chars)
        elif character_key:
            positive_prompt = _clean_foreign_character_tags(positive_prompt, character_key)
        positive_prompt, final_core_fidelity = _apply_prompt_core_fidelity(
            positive_prompt,
            prompt_request_text,
            protected_tags=protected_tags,
        )
        if final_core_fidelity.get("added") or final_core_fidelity.get("removed"):
            _smart_trace(
                "prompt_core_fidelity_applied",
                request_id=request_id,
                conversation_code=conversation_code,
                character_key=character_key,
                core_constraint_added_count=len(final_core_fidelity.get("added") or []),
                core_conflict_removed_count=len(final_core_fidelity.get("removed") or []),
            )
        plan_json = json.dumps(
            {
                "workflow_key": workflow_key,
                "resolution_key": resolution_key,
                "width": resolution["width"],
                "height": resolution["height"],
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "loras": loras,
                "fallback_level": fallback_level,
                "workflow_source": workflow_source,
                "character_key": character_key,
                "locked_character_tags": enforced.get("locked_character_tags") or [],
                "foreign_character_tags_removed_count": removed_foreign_count,
                "character_workflow_key": character_workflow_key,
                "allow_external_lora": bool(enforced.get("allow_external_lora")),
                "character_tag_injected": bool(enforced.get("character_tag_injected")),
                "internal_lora_count": int(enforced.get("internal_lora_count") or 0),
                "external_lora_count": int(enforced.get("external_lora_count") or 0),
                "workflow_health_status": str(enforced.get("workflow_health_status") or "unknown"),
                "selected_characters": selected_characters,
                "selected_characters_json": selected_characters_json,
                "conversation_code": conversation_code,
                "conversation_id": conversation_id,
                "character_tag_source": character_tag_source,
                "translated_character_name": translated_character_name,
                "original_character_name": original_character_name,
                "core_fidelity": final_core_fidelity,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # 构建结构化草稿 JSON
        structured_draft = json.dumps(
            {
                "scene": str(result.get("scene") or "").strip(),
                "style": str(result.get("style") or "").strip(),
                "clothing": str(result.get("clothing") or "").strip(),
                "expression": str(result.get("expression") or "").strip(),
                "action": str(result.get("action") or "").strip(),
                "composition": str(result.get("composition") or "").strip(),
                "mood": str(result.get("mood") or "").strip(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        draft = await asyncio.to_thread(
            save_smart_agent_prompt_draft,
            s,
            conversation_id=conversation_id,
            message_id=message_id,
            prompt_draft=positive_prompt,
            plan_json=plan_json,
            request_text=prompt_request_text,
            workflow_key=workflow_key,
            loras_json=json.dumps(loras, ensure_ascii=False, separators=(",", ":")),
            prompt_source="smart_agent+character_registry" if enforced.get("forced") else "smart_agent",
            character_key=character_key,
            workflow_source=workflow_source,
            fallback_level=fallback_level,
            width=int(resolution["width"]),
            height=int(resolution["height"]),
            structured_draft_json=structured_draft,
        )
        if prompt_text_requested:
            prompt_reply = _format_prompt_text_response(positive_prompt)
            add_conversation_message(
                s,
                conversation_id=conversation_id,
                role="assistant",
                content=prompt_reply,
                safe_content=prompt_reply,
            )
            _add_safe_smart_agent_event(
                s,
                conversation_id=conversation_id,
                event_type="assistant_message",
                public_message=prompt_reply,
            )
            _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")
            _smart_trace(
                "prompt_text_returned",
                request_id=request_id,
                conversation_code=conversation_code,
                character_key=character_key,
            )
            if message_id:
                mark_smart_agent_message_status(s, message_id=message_id, status="done")
                message_terminal = True
            return
        # 构建 prompt_ready 完成消息
        scene_text = str(result.get("scene") or "").strip()
        if scene_delegated and scene_text:
            ready_message = f"已为你选择场景:{scene_text}。\n\n提示词已整理完成,是否现在开始生成?"
        else:
            ready_message = "提示词已整理完成,是否现在开始生成?"
        add_conversation_message(s, conversation_id=conversation_id, role="assistant", content=ready_message, safe_content=ready_message)
        _add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="prompt_ready",
            public_message=json.dumps(
                {
                    "message": ready_message,
                    "prompt": positive_prompt,
                    "prompt_version": int(draft.get("prompt_version") or 1),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        _add_safe_smart_agent_event(s, conversation_id=conversation_id, event_type="done", public_message="")
        prompt_hash = hashlib.sha256(user_msg.encode("utf-8")).hexdigest()[:12]
        print(
            f"[SMART_AGENT] prompt_ready conversation={conversation_code} user_hash={hashlib.sha256(user_public_id.encode('utf-8')).hexdigest()[:12]} "
            f"prompt_hash={prompt_hash} prompt_len={len(user_msg)} workflow={workflow_key} source={workflow_source} "
            f"fallback={fallback_level} internal_loras={int(enforced.get('internal_lora_count') or 0)} "
            f"external_loras={int(enforced.get('external_lora_count') or 0)} resolution={resolution_key}",
            flush=True,
        )
        if message_id:
            mark_smart_agent_message_status(s, message_id=message_id, status="done")
            message_terminal = True
    except CharacterPromptValidationError as exc:
        _smart_trace("prompt_validation_failed", request_id=request_id, conversation_code=conversation_code,
                     error_code="character_prompt_validation", error_type=type(exc).__name__)
        _add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="error",
            public_message="人物提示词整理失败,请重新尝试;如果该人物不在人物库中,系统将使用翻译后的名称继续生成。",
            private_detail="prompt_validation_failed",
        )
        if message_id:
            mark_smart_agent_message_status(s, message_id=message_id, status="failed", error="prompt_validation_failed")
            message_terminal = True
    except Exception as exc:
        import traceback as _tb
        exc_type = type(exc).__name__
        tb_lines = _tb.format_exception(type(exc), exc, exc.__traceback__)
        tb_short = " | ".join(line.strip() for line in tb_lines[-3:])[:300]

        # 分类错误码 — 不记录密钥/Cookie/Token
        exc_msg = str(exc)[:200].lower()
        if "deepseek" in exc_type.lower() or "httpx" in exc_type.lower() or "DeepSeekError" in exc_type:
            error_code = "agent_unavailable"
            public_msg = "智能 Agent 暂时无法连接，请稍后重试。"
        elif "ollama" in exc_type.lower() or "translator" in exc_type.lower() or "translate" in exc_type.lower():
            # 翻译失败不应阻断整个流程，但如果是外层异常，则提示用户补充信息
            error_code = "character_translation_failed"
            public_msg = "人物名称翻译暂不可用。请尝试补充英文名或 Tag（如 nanami_mami）后重新发送。"
        elif "DB" in exc_type or "sqlite" in exc_type.lower() or "database" in exc_type.lower() or "OperationalError" in exc_type:
            error_code = "prompt_draft_save_failed"
            public_msg = "方案保存失败，请稍后重试。"
        elif "timeout" in exc_type.lower() or "timeout" in exc_msg:
            error_code = "agent_unavailable"
            public_msg = "智能 Agent 响应超时，请稍后重试。"
        elif "prompt" in exc_type.lower() or "tag" in exc_type.lower() or "assembly" in exc_type.lower():
            error_code = "prompt_assembly_failed"
            public_msg = "提示词组装失败，请尝试用更简洁的描述重新发送。"
        else:
            error_code = "smart_agent_internal_error"
            public_msg = "处理过程中出现错误，请稍后重试。如果问题持续，请尝试用英文 Tag 描述人物。"

        _smart_trace("smart_agent_unhandled", request_id=request_id, conversation_code=conversation_code,
                     error_code=error_code, error_type=exc_type)
        print(f"[SMART_AGENT_ERROR] error_code={error_code} exc={exc_type}: {str(exc)[:200]}\ntraceback={tb_short}", flush=True)
        _add_safe_smart_agent_event(
            s,
            conversation_id=conversation_id,
            event_type="error",
            public_message=public_msg,
            private_detail=f"{error_code}: {exc_type}",
        )
        if message_id:
            mark_smart_agent_message_status(s, message_id=message_id, status="failed", error=f"{error_code}:{exc_type}")
            message_terminal = True
    finally:
        if message_id and not message_terminal:
            try:
                mark_smart_agent_message_status(s, message_id=message_id, status="done")
            except Exception:
                pass



async def _process_smart_agent_chat_message(
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
    """Feature-flagged Smart Agent dispatcher.

    V2 owns only the conversational planning path. Queue creation, billing,
    task IDs and ComfyUI execution stay in the existing server/database code.
    """
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        await process_smart_agent_turn_v2(
            s=s,
            conversation_code=conversation_code,
            conversation_id=conversation_id,
            legacy_id=legacy_id,
            username=username,
            user_public_id=user_public_id,
            user_msg=user_msg,
            resolved_intent=resolved_intent,
            client_request_id=client_request_id,
            message_id=message_id,
        )
        return
    await _process_smart_agent_chat_message_legacy(
        s=s,
        conversation_code=conversation_code,
        conversation_id=conversation_id,
        legacy_id=legacy_id,
        username=username,
        user_public_id=user_public_id,
        user_msg=user_msg,
        resolved_intent=resolved_intent,
        client_request_id=client_request_id,
        message_id=message_id,
    )


async def smart_agent_chat_worker_loop() -> None:
    while True:
        try:
            item = await asyncio.to_thread(claim_next_smart_agent_chat_message, settings)
            if not item:
                await asyncio.sleep(0.8)
                continue
            await _process_smart_agent_chat_message(
                s=settings,
                conversation_code=str(item["conversation_code"]),
                conversation_id=int(item["conversation_id"]),
                legacy_id=str(item["legacy_user_id"]),
                username=str(item.get("username") or "Smart Agent User"),
                user_public_id=str(item.get("account_id") or item.get("legacy_user_id") or ""),
                user_msg=str(item.get("content") or ""),
                resolved_intent=str(item.get("intent") or "chat"),
                client_request_id=str(item.get("client_request_id") or "") or None,
                message_id=int(item["message_id"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[SMART_AGENT] chat_worker_error error={type(exc).__name__}", flush=True)
            await asyncio.sleep(2)


_INTERNAL_ERROR_MAP: dict[str, str] = {
    "prompt_draft_not_ready": "提示词尚未整理完成,请等待整理完成后再确认生成。",
    "invalid_prompt_state": "提示词状态异常,请重新描述需求。",
    "conversation_lock_failed": "系统繁忙,请稍后重试。",
    "missing_prompt_version": "提示词版本缺失,请重新整理需求。",
    "database_transition_failed": "系统错误,请稍后重试。",
    "余额不足": "你的 Credits 不足,请充值后再试。",
    "当前全局队列已满": "当前生成队列已满,请稍后重试。",
    "你当前未完成的任务太多,请等待或取消后再提交": "你当前未完成的任务太多,请等待或取消后再提交。",
    "character_prompt_validation_failed": "人物提示词整理失败,请重新尝试;如果该人物不在人物库中,系统将使用翻译后的名称继续生成。",
    "character_ambiguous": "识别到多个可能人物,请明确要生成哪一位。",
}


def _friendly_error(detail: str) -> str:
    """将内部错误码映射为安全的用户中文提示。"""
    detail_str = str(detail or "")
    if detail_str in _INTERNAL_ERROR_MAP:
        return _INTERNAL_ERROR_MAP[detail_str]
    # 检查是否匹配前缀
    for key, value in _INTERNAL_ERROR_MAP.items():
        if detail_str.startswith(key):
            return value
    # Fallback: 检查关键词
    if "prompt_draft_not_ready" in detail_str:
        return _INTERNAL_ERROR_MAP["prompt_draft_not_ready"]
    if "余额不足" in detail_str or "balance" in detail_str.lower():
        return "你的 Credits 不足,请充值后再试。"
    return f"操作失败,请稍后重试。"


def _is_confirmation_pending(conversation_id: int) -> bool:
    """检查会话是否有消息仍在 processing 状态。"""
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM smart_agent_messages "
            "WHERE conversation_id=? AND role='user' AND status='processing'",
            (int(conversation_id),),
        ).fetchone()
        return int(row["cnt"] if row else 0) > 0
    finally:
        conn.close()


def _character_from_key(character_key: str) -> dict[str, Any] | None:
    key = str(character_key or "").strip()
    if not key:
        return None
    for character in load_characters():
        if stable_character_key(character) == key:
            return character
    return None


async def _confirm_smart_agent_prompt_generation(
    *,
    s: Settings,
    conversation: dict[str, Any],
    conversation_code: str,
    legacy_id: str,
    username: str,
    user_public_id: str,
    client_request_id: str | None,
    request: Request | None = None,
    user: Any = None,
) -> dict[str, Any]:
    draft = await asyncio.to_thread(get_smart_agent_prompt_draft, s, conversation_id=int(conversation["id"]))
    if not draft or str(draft.get("status") or "") != "prompt_ready":
        if draft and draft.get("generation_job_code"):
            return {
                "ok": True,
                "already_created": True,
                "job_code": str(draft.get("generation_job_code")),
                "prompt_version": int(draft.get("prompt_version") or 1),
            }
        raise HTTPException(status_code=409, detail=_friendly_error("prompt_draft_not_ready"))
    try:
        loras = json.loads(str(draft.get("loras_json") or "[]"))
    except json.JSONDecodeError:
        loras = []
    character = _character_from_key(str(draft.get("resolved_character_key") or ""))
    try:
        validate_character_prompt(
            prompt=str(draft.get("prompt_draft") or ""),
            character=character,
            workflow_key=str(draft.get("workflow_key") or ""),
            loras=loras if isinstance(loras, list) else [],
            user_text=str(draft.get("request_text") or ""),
        )
    except CharacterPromptValidationError as exc:
        _add_safe_smart_agent_event(
            s,
            conversation_id=int(conversation["id"]),
            event_type="failed",
            public_message="人物提示词整理失败,请重新尝试;如果该人物不在人物库中,系统将使用翻译后的名称继续生成。",
            private_detail="character_prompt_validation_failed",
        )
        raise HTTPException(status_code=400, detail=_friendly_error("character_prompt_validation_failed")) from exc

    # Rate limit AFTER validation succeeds - failed validations don't consume quota
    if request is not None and user is not None:
        _rate_limit_smart_agent(request, user, s, action="generate")

    _add_safe_smart_agent_event(
        s,
        conversation_id=int(conversation["id"]),
        event_type="generating",
        public_message="正在创建生图任务......",
    )
    try:
        result = await asyncio.to_thread(
            confirm_smart_agent_prompt_draft_atomic,
            s,
            conversation_id=int(conversation["id"]),
            user_id=legacy_id,
            username=username,
            job_code=make_job_code(),
            cost_credits=int(s.smart_agent_cost_credits),
            conversation_code=conversation_code,
            client_request_id=client_request_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=_friendly_error(str(exc))) from exc

    job_code = str(result["job_code"])
    _add_safe_smart_agent_event(
        s,
        conversation_id=int(conversation["id"]),
        event_type="queued",
        job_code=job_code,
        public_message=f"任务已加入队列:{job_code}",
    )
    final_reply = ""
    _add_safe_smart_agent_event(
        s,
        conversation_id=int(conversation["id"]),
        event_type="generated",
        job_code=job_code,
        public_message=final_reply,
    )
    _add_safe_smart_agent_event(s, conversation_id=int(conversation["id"]), event_type="done", public_message="")
    redis_delete(s, "uma:cache:queue_status", f"uma:cache:tasks_summary:{legacy_id}", f"{TASK_SUMMARY_CACHE_PREFIX}:{legacy_id}")
    print(
        f"[SMART_AGENT] prompt_confirmed job={job_code} conversation={conversation_code} "
        f"user_hash={hashlib.sha256(user_public_id.encode('utf-8')).hexdigest()[:12]} "
        f"prompt_version={int(result.get('prompt_version') or 1)} already_created={bool(result.get('already_created'))}",
        flush=True,
    )
    return {
        "ok": True,
        "job_code": job_code,
        "already_created": bool(result.get("already_created")),
        "prompt_version": int(result.get("prompt_version") or 1),
    }


@app.post("/api/smart-agent/conversations/{conversation_code}/prompt-draft/generate")
async def confirm_smart_agent_prompt_draft(
    conversation_code: str,
    payload: SmartAgentGenerateRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        raise HTTPException(status_code=409, detail="Smart Agent V2 会在聊天回合内直接提交任务。")
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return await _confirm_smart_agent_prompt_generation(
        s=s,
        conversation=conv,
        conversation_code=conversation_code,
        legacy_id=legacy_id,
        username=user.username,
        user_public_id=user.user_id,
        client_request_id=request.headers.get("X-Client-Request-Id"),
        request=request,
        user=user,
    )


@app.post("/api/smart-agent/conversations/{conversation_code}/resolve-disambiguation")
async def resolve_character_disambiguation_endpoint(
    conversation_code: str,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    """结构化解决人物歧义:用户点击候选按钮后调用。"""
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    conv_id = int(conv["id"])

    # 解析请求体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求格式错误")
    character_key = str(body.get("character_key") or "").strip()
    if not character_key:
        raise HTTPException(status_code=400, detail="缺少 character_key")

    # 检查是否有待解决的歧义
    pending = get_pending_disambiguation_json(s, conversation_id=conv_id)
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待确认的人物歧义")

    # 验证 character_key 属于当前候选列表 — V2 结构：groups[].candidates[]
    group_id = str(body.get("group_id") or "").strip()
    matched_candidate = None
    matched_group_id = None
    for g in pending.get("groups", []):
        if g.get("status") != "pending":
            continue
        if group_id and g.get("group_id") != group_id:
            continue
        for c in g.get("candidates", []):
            c_key = str(c.get("character_key") or c.get("identity_key") or "")
            if c_key == character_key:
                matched_candidate = c
                matched_group_id = g["group_id"]
                break
        if matched_candidate:
            break
    if not matched_candidate:
        raise HTTPException(status_code=400, detail="该人物不在当前候选列表中")

    # 原子解决歧义 — 使用 V2 resolve_group
    char_name = str(matched_candidate.get("name_zh") or matched_candidate.get("name_en") or "")
    resolve_group(pending, matched_group_id, str(matched_candidate.get("identity_key") or character_key))
    save_pending_disambiguation_json(s, conversation_id=conv_id, disambiguation_json=pending)

    # 记录用户选择消息
    franchise = str(matched_candidate.get("franchise_zh") or matched_candidate.get("franchise") or "")
    user_choice_text = char_name
    add_conversation_message(
        s, conversation_id=conv_id, role="user",
        content=user_choice_text, safe_content=user_choice_text,
        status="done", intent="generate",
    )

    # 回复确认
    if franchise:
        confirm_msg = f"已确定人物:{char_name}(《{franchise}》)。正在继续整理生成方案……"
    else:
        confirm_msg = f"已确定人物:{char_name}。正在继续整理生成方案……"
    add_conversation_message(s, conversation_id=conv_id, role="assistant", content=confirm_msg, safe_content=confirm_msg)
    _add_safe_smart_agent_event(s, conversation_id=conv_id, event_type="assistant_message", public_message=confirm_msg)

    # 检查是否所有歧义组都已解决
    if all_groups_resolved(pending):
        # 组装已选角色名称
        all_selected = []
        for g in pending.get("groups", []):
            for c in g.get("candidates", []):
                if c.get("identity_key") == g.get("selected_identity_key"):
                    all_selected.append(c)
        resolved_names = [c.get("name_zh", c.get("name_en", "")) for c in all_selected]
        char_display = "、".join(resolved_names) if resolved_names else char_name

        # Preserve the exact server-validated character IDs for the queued message.
        # The worker must not re-run ambiguous name matching after a button click.
        original_request = str(pending.get("original_request") or "").strip()
        constraints = pending.get("constraints") or {}
        supplements = constraints.get("supplements") if isinstance(constraints, dict) else []
        if not isinstance(supplements, list):
            supplements = []
        queued_request = "\n".join(
            item for item in (original_request, *[str(x).strip() for x in supplements]) if item
        ) or original_request or char_display
        selected_character_ids = [
            str(c.get("identity_key") or c.get("character_key") or "").strip()
            for c in all_selected
        ]
        selected_character_ids = [item for item in selected_character_ids if item]

        clear_pending_disambiguation(s, conversation_id=conv_id)

        turn_request_id = (
            request.headers.get("X-Client-Request-Id")
            or f"smart-disambiguation:{conv_id}:{matched_group_id}:{character_key}"
        )[:100]
        message_id = add_conversation_message(
            s,
            conversation_id=conv_id,
            role="user",
            content=queued_request,
            safe_content=sanitize_public_agent_message(queued_request),
            status="pending",
            intent="generate",
            client_request_id=turn_request_id,
        )
        if selected_character_ids:
            save_message_resolution(
                s,
                conversation_id=conv_id,
                message_id=message_id,
                character_ids=selected_character_ids,
                source="disambiguation_button",
            )
        _add_safe_smart_agent_event(
            s,
            conversation_id=conv_id,
            event_type="message_pending",
            public_message="人物已确认，正在继续处理。",
        )

        return {
            "ok": True,
            "conversation_code": conversation_code,
            "character_key": character_key,
            "display_name": char_name,
            "message_id": message_id,
            "processing": True,
        }
    else:
        # 还有未解决的歧义组，等待其他组选择
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "character_key": character_key,
            "display_name": char_name,
            "pending_groups_remaining": True,
        }


async def _send_smart_agent_message_legacy(
    conversation_code: str,
    payload: SmartAgentMessageRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    """统一消息入口:对话 + 自动生成。

    DeepSeek 只做翻译/结构化;是否进入生成由本地策略决定。
    """
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    user_msg = payload.message.strip()
    client_request_id = request.headers.get("X-Client-Request-Id") or uuid.uuid4().hex

    # 幂等:同一个 client_request_id 不创建重复用户消息
    if client_request_id and client_request_id.strip():
        existing = _find_message_by_client_request_id(s, conversation_id=int(conv["id"]), client_request_id=client_request_id)
        if existing:
            return {
                "ok": True,
                "conversation_code": conversation_code,
                "message_id": int(existing["id"]),
                "resolved_intent": str(existing.get("intent") or "chat"),
                "should_create_task": str(existing.get("intent") or "chat") in {"generate", "regenerate", "edit"},
                "processing": str(existing.get("status") or "") == "pending",
                "duplicate": True,
            }

    draft = get_smart_agent_prompt_draft(s, conversation_id=int(conv["id"]))
    draft_ready = bool(draft and str(draft.get("status") or "") == "prompt_ready")
    prompt_text_requested = _is_prompt_text_request(user_msg)

    if prompt_text_requested:
        saved_prompt = _valid_saved_prompt_from_draft(draft)
        if saved_prompt:
            safe_user_msg = sanitize_public_agent_message(user_msg)
            message_id = add_conversation_message(
                s,
                conversation_id=conv["id"],
                role="user",
                content=user_msg,
                safe_content=safe_user_msg,
                status="done",
                intent="chat",
                client_request_id=client_request_id,
            )
            reply = _format_prompt_text_response(saved_prompt)
            add_conversation_message(s, conversation_id=conv["id"], role="assistant", content=reply, safe_content=reply)
            return {
                "ok": True,
                "conversation_code": conversation_code,
                "message_id": message_id,
                "resolved_intent": "chat",
                "should_create_task": False,
                "processing": False,
                "assistant_message": reply,
            }

    # 如果有待确认的人物歧义,直接排队到 worker 处理(不走 prompt_ready 确认逻辑)
    pending_dis_check_msg = get_pending_disambiguation(s, conversation_id=int(conv["id"]))
    if pending_dis_check_msg:
        resolved_intent_dis = _resolve_smart_agent_intent(user_msg)
        _rate_limit_smart_agent(request, user, s, action="chat")
        safe_user_msg = sanitize_public_agent_message(user_msg)
        message_id = add_conversation_message(
            s,
            conversation_id=conv["id"],
            role="user",
            content=user_msg,
            safe_content=safe_user_msg,
            status="pending",
            intent=resolved_intent_dis,
            client_request_id=client_request_id,
        )
        _add_safe_smart_agent_event(
            s,
            conversation_id=conv["id"],
            event_type="message_pending",
            public_message="消息已发送,等待处理。",
        )
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "message_id": message_id,
            "resolved_intent": resolved_intent_dis,
            "should_create_task": False,
            "processing": True,
        }

    if _is_prompt_ready_confirmation(user_msg, prompt_ready=draft_ready):
        safe_user_msg = sanitize_public_agent_message(user_msg)
        message_id = add_conversation_message(
            s,
            conversation_id=conv["id"],
            role="user",
            content=user_msg,
            safe_content=safe_user_msg,
            status="done",
            intent="generate",
            client_request_id=client_request_id,
        )
        result = await _confirm_smart_agent_prompt_generation(
            s=s,
            conversation=conv,
            conversation_code=conversation_code,
            legacy_id=legacy_id,
            username=user.username,
            user_public_id=user.user_id,
            client_request_id=f"prompt-confirm:{conversation_code}:{int(draft.get('prompt_version') or 1) if draft else 0}",
            request=request,
            user=user,
        )
        result["message_id"] = message_id
        result["resolved_intent"] = "generate"
        result["processing"] = False
        return result

    # 没有 prompt_ready 草稿但用户尝试确认生成 → 提示等待
    if _looks_like_confirmation_attempt(user_msg) and _is_confirmation_pending(int(conv["id"])):
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "intent": "generate",
            "processing": True,
            "waiting_for_draft": True,
            "assistant_message": "提示词还在整理中喵,整理完成后会请你确认生成~",
        }

    # 没有 prompt_ready 草稿时，不在入口层拦截明确生成指令。
    # 这类消息必须进入后台 worker，由本地意图 + 当前草稿/历史上下文决定
    # 是否能整理出新的 prompt_ready；否则会出现换人物后“开始生成”卡住。

    # "看看"/"整理好了吗" → 查看当前草稿状态,不调用 DS
    view_draft_patterns = ("看看", "看看方案", "看看提示词", "整理好了吗", "弄好了吗", "完成了吗")
    if user_msg in view_draft_patterns or any(
        user_msg.startswith(m) and (len(user_msg) == len(m) or user_msg[len(m)] in (" ", ",", "?", "?"))
        for m in view_draft_patterns
    ):
        # 检查是否有待确认的人物歧义
        pending_dis_check = get_pending_disambiguation(s, conversation_id=int(conv["id"]))
        if pending_dis_check:
            safe_user_msg = sanitize_public_agent_message(user_msg)
            add_conversation_message(
                s, conversation_id=conv["id"], role="user",
                content=user_msg, safe_content=safe_user_msg,
                status="done", intent="chat", client_request_id=client_request_id,
            )
            # 重新发送歧义确认事件(白名单过滤候选字段)
            import json as _json
            candidates = pending_dis_check.get("candidates", [])
            term = str(pending_dis_check.get("term") or "")
            public_candidates = []
            for c in candidates:
                public_candidates.append({
                    "character_key": str(c.get("character_key") or c.get("key") or ""),
                    "display_name": str(c.get("name_zh") or ""),
                    "display_name_en": str(c.get("name_en") or ""),
                    "franchise": str(c.get("franchise_en") or c.get("franchise_zh") or ""),
                })
            _add_safe_smart_agent_event(
                s,
                conversation_id=int(conv["id"]),
                event_type="character_disambiguation",
                public_message=_json.dumps(
                    {"term": term, "candidates": public_candidates},
                    ensure_ascii=False, separators=(",", ":"),
                ),
            )
            return {"ok": True, "conversation_code": conversation_code, "disambiguation_pending": True}
        if draft_ready:
            # 已有 prompt_ready 草稿 → 重新发出 prompt_ready 事件
            safe_user_msg = sanitize_public_agent_message(user_msg)
            add_conversation_message(
                s, conversation_id=conv["id"], role="user",
                content=user_msg, safe_content=safe_user_msg,
                status="done", intent="chat", client_request_id=client_request_id,
            )
            prompt_text = str(draft.get("prompt_draft") or "")
            ready_message = "提示词已整理完成,是否现在开始生成?"
            _add_safe_smart_agent_event(
                s,
                conversation_id=conv["id"],
                event_type="prompt_ready",
                public_message=json.dumps(
                    {
                        "message": ready_message,
                        "prompt": prompt_text,
                        "prompt_version": int(draft.get("prompt_version") or 1),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return {
                "ok": True,
                "conversation_code": conversation_code,
                "draft_ready": True,
                "prompt_version": int(draft.get("prompt_version") or 1),
            }
        elif _is_confirmation_pending(int(conv["id"])):
            # 还在处理中
            return {
                "ok": True,
                "conversation_code": conversation_code,
                "processing": True,
                "waiting_for_draft": True,
                "assistant_message": "提示词还在整理中,请稍等喵~",
            }
        else:
            # 没有草稿
            return {
                "ok": True,
                "conversation_code": conversation_code,
                "draft_ready": False,
                "assistant_message": "还没有整理好的方案喵,请先描述你想生成的图片~",
            }

    # "这个是啥场景"/"现在是什么场景" 等只读查询 → 从草稿直接回复,不调用 DS
    if _is_character_query(user_msg) and draft_ready:
        safe_user_msg = sanitize_public_agent_message(user_msg)
        add_conversation_message(
            s, conversation_id=conv["id"], role="user",
            content=user_msg, safe_content=safe_user_msg,
            status="done", intent="chat", client_request_id=client_request_id,
        )
        query_response = _build_character_query_response(draft)
        if query_response:
            add_conversation_message(s, conversation_id=conv["id"], role="assistant", content=query_response, safe_content=query_response)
            _add_safe_smart_agent_event(s, conversation_id=conv["id"], event_type="assistant_message", public_message=query_response)
            return {"ok": True, "conversation_code": conversation_code, "draft_ready": True}

    if not s.deepseek_api_key:
        raise HTTPException(status_code=503, detail="Smart Agent 暂未配置,请稍后再试")
    resolved_intent = _resolve_smart_agent_intent(user_msg)
    should_create_task = resolved_intent in {"generate", "regenerate", "edit"}
    _rate_limit_smart_agent(request, user, s, action="generate" if should_create_task else "chat")
    safe_user_msg = sanitize_public_agent_message(user_msg)
    message_id = add_conversation_message(
        s,
        conversation_id=conv["id"],
        role="user",
        content=user_msg,
        safe_content=safe_user_msg,
        status="pending",
        intent=resolved_intent,
        client_request_id=client_request_id,
    )
    _add_safe_smart_agent_event(
        s,
        conversation_id=conv["id"],
        event_type="message_pending",
        public_message="消息已发送,等待处理。",
    )
    return {
        "ok": True,
        "conversation_code": conversation_code,
        "message_id": message_id,
        "resolved_intent": resolved_intent,
        "should_create_task": should_create_task,
        "processing": True,
    }



@app.post("/api/smart-agent/conversations/{conversation_code}/messages")
async def send_smart_agent_message(
    conversation_code: str,
    payload: SmartAgentMessageRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    """V2 message entry with one active user turn per conversation."""
    if not bool(getattr(s, "smart_agent_v2_enabled", False)):
        return await _send_smart_agent_message_legacy(
            conversation_code=conversation_code,
            payload=payload,
            request=request,
            csrf=csrf,
            user=user,
            s=s,
        )
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")

    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    user_msg = str(payload.message or "").strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="请输入消息")
    client_request_id = (request.headers.get("X-Client-Request-Id") or uuid.uuid4().hex)[:100]

    existing = _find_message_by_client_request_id(
        s,
        conversation_id=int(conv["id"]),
        client_request_id=client_request_id,
    )
    if existing:
        status = str(existing.get("status") or "")
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "message_id": int(existing["id"]),
            "resolved_intent": str(existing.get("intent") or "chat"),
            "processing": status in {"pending", "processing"},
            "duplicate": True,
        }

    resolved_intent = _resolve_smart_agent_intent(user_msg)
    turn = prepare_turn(
        user_msg,
        resolved_intent=resolved_intent,
        client_request_id=client_request_id,
    )

    if has_active_turn(s, conversation_id=int(conv["id"])):
        raise HTTPException(status_code=409, detail="智能 Agent 正在处理上一条消息，请稍后。")

    # Internal prompts are never returned in V2, even when a legacy draft exists.
    if turn.prompt_exposure_requested:
        safe_user_msg = sanitize_public_agent_message(user_msg)
        message_id = add_conversation_message(
            s,
            conversation_id=int(conv["id"]),
            role="user",
            content=user_msg,
            safe_content=safe_user_msg,
            status="done",
            intent="chat",
            client_request_id=client_request_id,
        )
        reply = safe_prompt_hidden_reply()
        add_conversation_message(
            s,
            conversation_id=int(conv["id"]),
            role="assistant",
            content=reply,
            safe_content=reply,
        )
        _add_safe_smart_agent_event(
            s,
            conversation_id=int(conv["id"]),
            event_type="assistant_message",
            public_message=reply,
        )
        _add_safe_smart_agent_event(
            s,
            conversation_id=int(conv["id"]),
            event_type="done",
            public_message="",
        )
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "message_id": message_id,
            "resolved_intent": "chat",
            "processing": False,
        }

    draft = get_smart_agent_prompt_draft(s, conversation_id=int(conv["id"]))
    draft_ready = bool(draft and str(draft.get("status") or "") == "prompt_ready")

    # A bare "generate" uses the already prepared private draft immediately.
    if turn.generation_requested and turn.meta_only and draft_ready:
        _rate_limit_smart_agent(request, user, s, action="generate")
        message_id = add_conversation_message(
            s,
            conversation_id=int(conv["id"]),
            role="user",
            content=user_msg,
            safe_content=sanitize_public_agent_message(user_msg),
            status="done",
            intent="generate",
            client_request_id=client_request_id,
        )
        result = await _confirm_smart_agent_prompt_generation(
            s=s,
            conversation=conv,
            conversation_code=conversation_code,
            legacy_id=legacy_id,
            username=user.username,
            user_public_id=user.user_id,
            client_request_id=f"smart-v2-confirm:{conversation_code}:{int(draft.get('prompt_version') or 1)}",
            request=request,
            user=user,
        )
        result.update({
            "message_id": message_id,
            "resolved_intent": "generate",
            "processing": False,
        })
        return result

    if not s.deepseek_api_key:
        raise HTTPException(status_code=503, detail="Smart Agent 暂未配置，请稍后再试")

    _rate_limit_smart_agent(
        request,
        user,
        s,
        action="generate" if turn.generation_requested else "chat",
    )
    accepted = begin_turn_atomic(
        s,
        conversation_id=int(conv["id"]),
        client_request_id=client_request_id,
        generation_requested=turn.generation_requested,
        turn_id=turn.turn_key,
    )
    if accepted.get("duplicate"):
        return {
            "ok": True,
            "conversation_code": conversation_code,
            "message_id": accepted.get("message_id"),
            "resolved_intent": resolved_intent,
            "processing": str(accepted.get("status") or "") in {"accepted", "processing"},
            "duplicate": True,
        }

    try:
        message_id = add_conversation_message(
            s,
            conversation_id=int(conv["id"]),
            role="user",
            content=user_msg,
            safe_content=sanitize_public_agent_message(user_msg),
            status="pending",
            intent=resolved_intent,
            client_request_id=client_request_id,
        )
        bind_turn_message(s, turn_id=str(accepted["turn_id"]), message_id=message_id)
    except Exception as exc:
        abort_turn(s, turn_id=str(accepted["turn_id"]), error=type(exc).__name__)
        raise

    _add_safe_smart_agent_event(
        s,
        conversation_id=int(conv["id"]),
        event_type="message_pending",
        public_message="消息已发送，等待处理。",
    )
    return {
        "ok": True,
        "conversation_code": conversation_code,
        "message_id": message_id,
        "resolved_intent": resolved_intent,
        "should_create_task": turn.generation_requested,
        "processing": True,
        "turn_id": str(accepted["turn_id"]),
    }


@app.get("/api/smart-agent/conversations/{conversation_code}/events")
def get_smart_agent_events(
    conversation_code: str,
    after_id: int = 0,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    events = get_conversation_events(s, conversation_id=conv["id"], after_id=after_id)
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        for event in events:
            if str(event.get("event_type") or "") != "prompt_ready":
                continue
            prompt_version = 1
            try:
                old_payload = json.loads(str(event.get("public_message") or "{}"))
                prompt_version = int(old_payload.get("prompt_version") or 1)
            except Exception:
                pass
            event["public_message"] = json.dumps(
                {"message": "方案已准备完成。", "prompt_version": prompt_version},
                ensure_ascii=False,
                separators=(",", ":"),
            )
    return {
        "ok": True,
        "events": [
            {
                "id": e["id"],
                "event_type": e["event_type"],
                "public_message": e["public_message"],
                "job_code": e.get("job_code") or "",
                "created_at": e.get("created_at"),
            }
            for e in events
        ],
    }


@app.post("/api/smart-agent/conversations/{conversation_code}/clear")
def clear_conversation_memory(
    conversation_code: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    """清空当前对话的所有消息、事件和记忆,但不删除 conversation 本身。"""
    if not s.smart_agent_enabled:
        raise HTTPException(status_code=403, detail="Smart Agent 暂未开放")
    legacy_id = get_legacy_user_id_for_session(user, s)
    conv = get_conversation(s, conversation_code=conversation_code, legacy_user_id=legacy_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    if bool(getattr(s, "smart_agent_v2_enabled", False)):
        clear_v2_state(s, conversation_id=int(conv["id"]))
    clear_conversation(s, conversation_id=conv["id"])
    return {"ok": True}


async def _save_upload(s: Settings, upload: UploadFile, job_code: str) -> str:
    raw = await upload.read(s.max_input_image_bytes + 1)
    await upload.close()
    if len(raw) > s.max_input_image_bytes:
        raise HTTPException(status_code=413, detail="图片超过 12MB")
    try:
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw))
        image.seek(0)
        if image.width * image.height > 24_000_000:
            raise HTTPException(status_code=400, detail="图片像素过大")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    except UnidentifiedImageError as exc:
        raise HTTPException(status_code=400, detail="上传文件不是有效图片") from exc
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=400, detail="图片尺寸异常") from exc

    target = s.input_image_dir / f"{job_code}.png"
    image.save(target, format="PNG", optimize=True)
    return str(target)


def _assemble_multi_character_prompt(
    *,
    characters: list[dict[str, Any]],
    user_prompt: str,
) -> tuple[str, bool]:
    """为多人物合并 canonical tag,去重后返回合并 prompt。

    返回 (merged_prompt, is_multi_character)。
    自动根据去重后的明确人物加入人数标签(2girls / 3girls / 1girl,1boy 等)。
    """
    locked_tags: list[str] = []
    seen_tag_keys: set[str] = set()
    for character in characters:
        for tag in locked_character_tags(character):
            key = tag.lower().replace("_", " ").strip()
            if key in seen_tag_keys:
                continue
            seen_tag_keys.add(key)
            locked_tags.append(tag)
    if not locked_tags:
        final_prompt, _, _ = sanitize_inferred_appearance_tags(user_prompt, user_text=user_prompt, character=None)
        return final_prompt, False
    scene_clean = user_prompt
    for character in characters:
        scene_clean, _ = remove_foreign_character_tags(scene_clean, selected_character=character)
    appearance_clean, _, _ = sanitize_inferred_appearance_tags(
        scene_clean, user_text=user_prompt, character=characters[0],
    )
    scene_tags = [
        tag for tag in split_prompt_tags(appearance_clean)
        if tag.lower().replace("_", " ").strip() not in {
            t.lower().replace("_", " ").strip() for t in locked_tags
        }
    ]
    merged = _merge_tags_dedupe([", ".join(locked_tags), ", ".join(scene_tags)])
    base_prompt = ", ".join(merged)
    # ── 人数标签:删除旧的,加入正确的 ──
    final_prompt = _apply_count_tags(base_prompt, characters)
    return final_prompt, True


def _merge_tags_dedupe(groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for tag in _split_prompt_tags(group):
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)
    return merged


def _parse_character_resolution_payload(raw_payload: str | None) -> dict[str, Any] | None:
    raw = str(raw_payload or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_character_resolution") from exc
    if not isinstance(parsed, dict):
        raise ValueError("invalid_character_resolution")
    return parsed


def _prepare_translation_agent_character_resolution(
    prompt: str,
    raw_resolution_payload: str | None,
) -> tuple[str, str]:
    """Resolve form Agent character ambiguity before charging/enqueueing.

    Returns (prompt_source, character_key_field).  The character_key field is
    consumed later by the worker-side translation Agent to inject only confirmed
    character tags.  Ambiguous mentions return 409 to the browser and do not
    create a queue task.
    """
    resolution = _parse_character_resolution_payload(raw_resolution_payload)
    if resolution:
        validated = validate_character_resolution(prompt, resolution)
        character_ids = [
            str(item.get("characterId") or "").strip()
            for item in validated.get("resolvedCharacters", []) or []
            if str(item.get("characterId") or "").strip()
        ]
        skipped = list(validated.get("skippedMentions") or [])
        if character_ids:
            return "agent_character_resolved", serialize_character_ids(character_ids)
        if skipped:
            return "agent_character_no_library", "[]"
        return "agent_no_character", "[]"

    parsed = analyze_character_mentions(prompt)
    if parsed.get("status") in {"ambiguous", "mixed"}:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "code": "character_resolution_required",
                "message": "请选择具体人物后继续生成",
                "requiresCharacterSelection": True,
                "resolution": parsed,
                "characterResolution": parsed,
            },
        )
    character_ids = [
        str(item.get("characterId") or "").strip()
        for item in parsed.get("resolvedCharacters", []) or []
        if str(item.get("characterId") or "").strip()
    ]
    if character_ids:
        return "agent_character_resolved", serialize_character_ids(character_ids)
    return "agent_no_character", "[]"


@app.post("/api/tasks")
async def create_task(
    mode: str = Form("txt2img"),
    style_key: str = Form("style_a"),
    prompt: str = Form(""),
    width: int = Form(1024),
    height: int = Form(1536),
    lora_weight: float = Form(1.0),
    denoise: float = Form(0.5),
    control_type: str = Form("depth"),
    control_character: str = Form("prompt"),
    auto_tagger: bool = Form(False),
    use_agent: bool = Form(False),
    client_request_id: str | None = Form(default=None),
    character_resolution: str | None = Form(default=None),
    original_prompt: str | None = Form(default=None),
    prompt_source: str | None = Form(default=None),
    fast_translation_request_code: str | None = Form(default=None),
    translation_mode: str = Form("none"),
    mock_result: str | None = Form(default=None),
    input_image: UploadFile | None = File(default=None),
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    if style_key == "anima":
        style_key = "anima_owner"
    try:
        width, height = validate_resolution(width, height)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    legacy_id = get_legacy_user_id_for_session(user, s)
    input_path = None

    # Server determines translation_mode, not client
    server_translation_mode = str(translation_mode or "none").strip().lower()
    if server_translation_mode not in {"none", "normal", "fast"}:
        server_translation_mode = "none"

    # Fast translation path: single atomic call
    if server_translation_mode == "fast":
        if not s.fast_translator_enabled:
            raise HTTPException(status_code=400, detail="极速翻译当前未启用")

        raw_prompt = (original_prompt or prompt or "").strip()

        # Parse character resolution
        char_keys: list[str] = []
        char_decision = "none"
        if character_resolution:
            try:
                res_obj = json.loads(character_resolution)
                if isinstance(res_obj, dict):
                    from .smart_agent.disambiguation_engine import validate_character_resolution, analyze_character_mentions
                    try:
                        validated = validate_character_resolution(raw_prompt, res_obj)
                        char_keys = [
                            str(item.get("characterId") or item.get("key") or "").strip()
                            for item in validated.get("resolvedCharacters", []) or []
                            if str(item.get("characterId") or item.get("key") or "").strip()
                        ]
                        char_keys = list(dict.fromkeys(char_keys))
                        char_decision = "resolved" if char_keys else "none"
                    except ValueError:
                        pass
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            # No resolution provided: analyze for ambiguity
            from .smart_agent.disambiguation_engine import analyze_character_mentions
            parsed = analyze_character_mentions(raw_prompt)
            if parsed.get("status") in {"ambiguous", "mixed"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "ok": False,
                        "code": "character_resolution_required",
                        "message": "请选择具体人物后继续生成",
                        "requiresCharacterSelection": True,
                        "resolution": parsed,
                        "characterResolution": parsed,
                    },
                )
            # Auto-resolve from library
            char_keys = [
                str(item.get("characterId") or "").strip()
                for item in parsed.get("resolvedCharacters", []) or []
                if str(item.get("characterId") or "").strip()
            ]
            char_keys = list(dict.fromkeys(char_keys))
            char_decision = "library" if char_keys else "none"

        job_code = make_job_code()
        if input_image and input_image.filename:
            input_path = await _save_upload(s, input_image, job_code)

        safe_mock_result = ""
        if s.is_local_env() and s.dev_auth_bypass:
            candidate = str(mock_result or "").strip().lower()
            if candidate in {"success", "failed", "timeout"}:
                safe_mock_result = candidate

        try:
            result = create_fast_translation_task_atomic(
                s,
                job_code=job_code,
                user_id=legacy_id,
                username=user.username,
                original_prompt=(original_prompt or prompt or "").strip(),
                translation_mode="fast",
                style_key=style_key,
                lora_weight=float(lora_weight),
                width=width,
                height=height,
                mode=mode,
                input_image_path=input_path,
                denoise=float(denoise),
                control_type=control_type,
                control_character=control_character,
                auto_tagger=bool(auto_tagger),
                character_keys=char_keys,
                character_resolution_decision=char_decision,
                client_request_id=client_request_id,
                mock_result=safe_mock_result,
                workflow_key=style_key,
            )
            print(f"[WEB] created fast translation job={job_code} provider={user.provider} source=web")
            return result
        except RuntimeError as exc:
            err_msg = str(exc)
            if err_msg == "active_task_limit":
                safe_cleanup_input(s, input_path)
                raise HTTPException(status_code=429, detail={"code": "active_task_limit", "message": "你当前未完成的任务太多，请等待或取消后再提交"}) from exc
            if err_msg == "generation_rate_limited":
                safe_cleanup_input(s, input_path)
                raise HTTPException(status_code=429, detail={"code": "generation_rate_limited", "message": "提交过于频繁，请稍后重试"}) from exc
            if err_msg == "queue_full":
                safe_cleanup_input(s, input_path)
                raise HTTPException(status_code=429, detail={"code": "queue_full", "message": "当前生成队列已满，请稍后重试"}) from exc
            if err_msg == "insufficient_credits":
                safe_cleanup_input(s, input_path)
                raise HTTPException(status_code=402, detail={"code": "insufficient_credits", "message": "Credits 不足，请充值后重试"}) from exc
            safe_cleanup_input(s, input_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            err_msg = str(exc)
            if err_msg == "client_request_id_conflict":
                safe_cleanup_input(s, input_path)
                raise HTTPException(status_code=409, detail={"code": "client_request_id_conflict", "message": "The same client_request_id was already used with different request content."}) from exc
            safe_cleanup_input(s, input_path)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            safe_cleanup_input(s, input_path)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception:
            safe_cleanup_input(s, input_path)
            raise

    # Normal/none translation path (existing flow)
    try:
        task_prompt = prompt.strip()
        task_prompt_source = ""
        task_character_key = ""
        if not (bool(use_agent) and s.agent_enabled):
            # ── 直通模式:原文原样写入,不做人物匹配/校验/Tag注入 ──
            if not task_prompt:
                raise ValueError("prompt_required")
            # 拒绝控制字符(保留换行和空格)
            if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in task_prompt):
                raise ValueError("prompt_contains_invalid_characters")
            # 限制最大长度
            if len(task_prompt) > 3000:
                raise ValueError("prompt_too_long")
            task_prompt_source = str(prompt_source or "").strip() or "user_raw"
            task_character_key = ""
        else:
            if not task_prompt:
                raise ValueError("prompt_required")
            if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in task_prompt):
                raise ValueError("prompt_contains_invalid_characters")
            if len(task_prompt) > 3000:
                raise ValueError("prompt_too_long")
            task_prompt_source, task_character_key = _prepare_translation_agent_character_resolution(
                task_prompt,
                character_resolution,
            )
        job_code = make_job_code()
        if input_image and input_image.filename:
            input_path = await _save_upload(s, input_image, job_code)
        safe_mock_result = ""
        if s.is_local_env() and s.dev_auth_bypass:
            candidate = str(mock_result or "").strip().lower()
            if candidate in {"success", "failed", "timeout"}:
                safe_mock_result = candidate
        # Rate limit: only count non-deduped new requests
        # Check if this client_request_id already exists (dedup) before counting
        _is_dedup = False
        if client_request_id:
            _cid = str(client_request_id).strip()[:80]
            if _cid:
                _conn = connect(s)
                try:
                    _existing = _conn.execute(
                        "SELECT 1 FROM generation_tasks WHERE user_id=? AND client_request_id=?",
                        (legacy_id, _cid),
                    ).fetchone()
                    _is_dedup = bool(_existing)
                finally:
                    _conn.close()
        if not _is_dedup:
            limiter.check(
                f"create:{user.user_id}",
                limit=max(1, int(getattr(s, 'generation_submit_user_limit', 20) or 20)),
                window_seconds=max(1, int(getattr(s, 'generation_submit_window_seconds', 60) or 60)),
            )
        result = create_task_atomic(
            s,
            job_code=job_code,
            user_id=legacy_id,
            username=user.username,
            prompt=task_prompt,
            style_key=style_key,
            lora_weight=float(lora_weight),
            width=width,
            height=height,
            mode=mode,
            input_image_path=input_path,
            denoise=float(denoise),
            control_type=control_type,
            control_character=control_character,
            auto_tagger=bool(auto_tagger),
            use_agent=bool(use_agent) and s.agent_enabled,
            client_request_id=client_request_id,
            prompt_source=task_prompt_source,
            character_key=task_character_key,
            mock_result=safe_mock_result,
            original_prompt=(original_prompt or "").strip()[:3000] or None,
            fast_translation_request_code=str(fast_translation_request_code or "").strip() or None,
        )
        print(f"[WEB] created job={job_code} provider={user.provider} source=web")
        return result
    except PermissionError as exc:
        safe_cleanup_input(s, input_path)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        safe_cleanup_input(s, input_path)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        safe_cleanup_input(s, input_path)
        raise


@app.post("/api/tasks/{job_code}/cancel")
def cancel(
    job_code: str,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    limiter.check(
        f"cancel:{user.user_id}",
        limit=max(1, int(getattr(s, 'cancel_submit_user_limit', 60) or 60)),
        window_seconds=max(1, int(getattr(s, 'cancel_submit_window_seconds', 60) or 60)),
    )
    legacy_id = get_legacy_user_id_for_session(user, s)
    try:
        result = cancel_task_atomic(s, legacy_id, job_code.upper())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    safe_cleanup_input(s, result.get("input_image_path"))
    return {
        "ok": True,
        "job_code": result["job_code"],
        "status": result["status"],
        "refunded_fen": result["refunded_fen"],
        "balance_fen": result["balance_fen"],
        "already_cancelled": result["already_cancelled"],
    }


@app.get("/api/outputs/{output_id}")
def output_file(output_id: int, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    row = get_output_owned(s, legacy_id, output_id)
    if not row:
        print(f"[OUTPUT_API] output_id={output_id} result=404 reason=not_owner", flush=True)
        raise HTTPException(status_code=404, detail="图片不存在")
    response = _serve_output_row(row, s)
    print(f"[OUTPUT_API] output_id={output_id} result=200 content_type={response.media_type}", flush=True)
    return response


@app.get("/api/tasks/latest")
def latest_task(user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    """Get the single most recent task for the current user."""
    legacy_id = get_legacy_user_id_for_session(user, s)
    request_id = uuid.uuid4().hex[:12]
    try:
        task = get_latest_relevant_task(s, legacy_id)
    except Exception as exc:
        print(
            f"[TASK_API] request_id={request_id} endpoint=latest result=500 error={type(exc).__name__}",
            flush=True,
        )
        raise HTTPException(status_code=503, detail="服务器暂时无法读取当前任务") from exc
    return {"ok": True, "task": task, "item": task}


@app.get("/api/tasks/history")
def history_tasks(
    limit: int = 20,
    offset: int = 0,
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, s)
    limit = min(max(limit, 1), 50)
    offset = max(offset, 0)
    items, has_more = list_user_tasks_paginated(s, legacy_id, limit=limit, offset=offset)
    return {"items": items, "has_more": has_more}


@app.get("/api/tasks/{job_code}/position")
def task_position(job_code: str, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    position = get_task_queue_position(s, legacy_id, job_code)
    if not position:
        raise HTTPException(status_code=404, detail="任务不存在")
    return position


@app.get("/api/tasks/{job_code}")
def get_task(job_code: str, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    """Get a specific task by job_code, scoped to the current user. Returns 404 if not found or not owned."""
    legacy_id = get_legacy_user_id_for_session(user, s)
    request_id = uuid.uuid4().hex[:12]
    try:
        task = get_task_by_job_code(s, legacy_id, job_code)
    except Exception as exc:
        print(
            f"[TASK_API] request_id={request_id} endpoint=detail job={job_code.upper()} result=500 error={type(exc).__name__}",
            flush=True,
        )
        raise HTTPException(status_code=503, detail="服务器暂时无法读取当前任务") from exc
    if not task:
        print(f"[TASK_API] job={job_code.upper()} result=404", flush=True)
        raise HTTPException(status_code=404, detail="任务不存在")
    print(
        f"[TASK_API] job={task['job_code']} status={task.get('status')} outputs={len(task.get('outputs') or [])}",
        flush=True,
    )
    return {"ok": True, "task": task, "item": task}


@app.get("/api/outputs/{output_id}/download")
def download_output(output_id: int, user: UserSession = Depends(get_current_user), s: Settings = Depends(get_settings)):
    legacy_id = get_legacy_user_id_for_session(user, s)
    row = get_output_owned(s, legacy_id, output_id)
    if not row:
        print(f"[OUTPUT_API] output_id={output_id} result=404 reason=not_owner", flush=True)
        raise HTTPException(status_code=404, detail="图片不存在")
    job_code = row["job_code"]
    try:
        path = s.resolve_output_path(row["file_path"])
    except (ValueError, FileNotFoundError):
        print(f"[OUTPUT_API] output_id={output_id} result=404 reason=missing_file", flush=True)
        raise HTTPException(status_code=404, detail="图片文件不存在")
    ext = path.suffix or ".png"
    download_name = f"UMA_{job_code}{ext}"
    response = _serve_output_row(row, s, download_name=download_name)
    print(f"[OUTPUT_API] output_id={output_id} result=200 content_type={response.media_type}", flush=True)
    return response


@app.post("/api/agent/refine", response_model=PromptRefineResponse)
async def agent_refine(
    body: PromptRefineRequest,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    s: Settings = Depends(get_settings),
):
    limiter.check(f"agent:{user.user_id}", limit=10, window_seconds=60)
    try:
        result = await refine_prompt(s, body.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return PromptRefineResponse(prompt=result)


@app.get("/api/debug/version")
def debug_version():
    return {"version": "resolve_output_path_v2", "ok": True}
