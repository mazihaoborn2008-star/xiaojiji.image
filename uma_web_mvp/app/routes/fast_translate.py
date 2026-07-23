from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.auth import get_current_user, get_legacy_user_id_for_session, require_csrf
from app.config import Settings, get_settings
from app.main_limiter import limiter
from app.schemas import UserSession
from app.services.fast_translator_service import CharacterSelectionRequired, ClientRequestIdConflict, FastTranslatorError, fast_refine_prompt

router = APIRouter(prefix="/api/prompt", tags=["fast-translate"])


class FastRefineRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    client_request_id: str | None = Field(default=None, max_length=80)
    resolved_character_ids: list[str] = Field(default_factory=list)
    character_resolution: dict[str, Any] | None = None


@router.post("/fast-refine")
async def fast_refine(
    body: FastRefineRequest,
    response: Response,
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    if not settings.fast_translator_enabled:
        raise HTTPException(status_code=404, detail="极速翻译当前未启用")
    limiter.check(
        f"fast_translate:{user.user_id}",
        limit=max(1, int(settings.ai_support_rate_limit_per_minute or 10)),
        window_seconds=60,
    )
    legacy_id = get_legacy_user_id_for_session(user, settings)
    resolution = body.character_resolution
    if not resolution and body.resolved_character_ids:
        resolution = {
            "status": "resolved",
            "selections": [{"characterId": item} for item in body.resolved_character_ids],
        }
    try:
        result = await fast_refine_prompt(
            settings,
            user_id=legacy_id,
            text=body.text,
            client_request_id=body.client_request_id,
            character_resolution=resolution,
        )
    except CharacterSelectionRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "code": exc.code,
                "message": exc.message,
                "requiresCharacterSelection": True,
                "resolution": exc.resolution,
                "characterResolution": exc.resolution,
            },
        ) from exc
    except ClientRequestIdConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "code": exc.code, "message": exc.message},
        ) from exc
    except FastTranslatorError as exc:
        status = 402 if exc.code == "insufficient_credits" else 400
        raise HTTPException(status_code=status, detail={"ok": False, "code": exc.code, "message": exc.message}) from exc
    return {
        "ok": result.ok,
        "prompt": result.prompt,
        "translation_mode": result.translation_mode,
        "character_match_source": result.character_match_source,
        "character_keys": result.character_keys,
        "charged_credits": result.charged_credits,
        "request_code": result.request_code,
        "status": result.status,
    }
