from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import get_current_user, get_legacy_user_id_for_session, require_csrf
from app.config import Settings, get_settings
from app.main_limiter import limiter
from app.schemas import UserSession
from app.services.ai_support_service import (
    AiSupportError,
    clear_ai_support_conversation,
    create_ai_support_conversation,
    get_ai_support_conversation,
    list_ai_support_conversations,
    send_ai_support_message,
)

router = APIRouter(prefix="/api/ai-support", tags=["ai-support"])


class AiSupportMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _guard(settings: Settings, user: UserSession, request: Request) -> None:
    if not settings.ai_support_enabled:
        raise HTTPException(status_code=404, detail="AI 客服当前未启用")
    limiter.check(
        f"ai_support:user:{user.user_id}",
        limit=max(1, int(settings.ai_support_rate_limit_per_minute or 10)),
        window_seconds=60,
    )
    client_host = request.client.host if request.client else "unknown"
    limiter.check(
        f"ai_support:ip:{client_host}",
        limit=max(20, int(settings.ai_support_rate_limit_per_minute or 10) * 5),
        window_seconds=60,
    )


@router.post("/conversations")
def create_conversation(
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    _guard(settings, user, request)
    legacy_id = get_legacy_user_id_for_session(user, settings)
    return {"ok": True, "conversation": create_ai_support_conversation(settings, legacy_id)}


@router.get("/conversations")
def conversations(user: UserSession = Depends(get_current_user), settings: Settings = Depends(get_settings)):
    if not settings.ai_support_enabled:
        raise HTTPException(status_code=404, detail="AI 客服当前未启用")
    legacy_id = get_legacy_user_id_for_session(user, settings)
    return {"ok": True, "conversations": list_ai_support_conversations(settings, legacy_id)}


@router.get("/conversations/{conversation_code}")
def conversation_detail(
    conversation_code: str,
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    legacy_id = get_legacy_user_id_for_session(user, settings)
    try:
        return {"ok": True, **get_ai_support_conversation(settings, legacy_id, conversation_code)}
    except AiSupportError as exc:
        status = 404 if exc.code in {"not_found", "ai_support_disabled"} else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc


@router.post("/conversations/{conversation_code}/messages")
async def send_message(
    conversation_code: str,
    body: AiSupportMessageRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    _guard(settings, user, request)
    legacy_id = get_legacy_user_id_for_session(user, settings)
    try:
        return await send_ai_support_message(
            settings,
            user_id=legacy_id,
            conversation_code=conversation_code,
            message=body.message,
        )
    except AiSupportError as exc:
        status = 404 if exc.code in {"not_found", "ai_support_disabled"} else 400
        raise HTTPException(status_code=status, detail=exc.message) from exc


@router.post("/conversations/{conversation_code}/clear")
def clear_conversation(
    conversation_code: str,
    request: Request,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    _guard(settings, user, request)
    legacy_id = get_legacy_user_id_for_session(user, settings)
    try:
        clear_ai_support_conversation(settings, legacy_id, conversation_code)
        return {"ok": True}
    except AiSupportError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc

