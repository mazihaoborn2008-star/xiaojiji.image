import asyncio
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from argon2.low_level import Type
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import Settings, get_settings
from .db import (
    bind_account_legacy_user,
    connect,
    consume_oauth_login_state,
    cleanup_oauth_login_states,
    clear_password_failures,
    create_oauth_login_state,
    create_account_session,
    create_password_reset_token,
    create_secure_email_code,
    create_secure_email_login_code,
    consume_password_reset_token,
    count_recent_password_attempts,
    create_email_account,
    email_account_has_password,
    email_identity_digest,
    get_active_account_session,
    find_or_create_discord_account,
    find_or_create_email_account,
    get_email_password_credential,
    get_email_account_by_hmac,
    get_account_by_id,
    get_account_legacy_user_id,
    grant_welcome_bonus_if_needed,
    mark_account_login,
    normalize_email_for_identity,
    record_email_password_attempt,
    register_password_failure,
    revoke_account_session,
    revoke_all_account_sessions,
    touch_account_session,
    upsert_email_password_credential,
    upsert_account_email_identity,
    validate_session_csrf,
    verify_secure_email_code,
    verify_secure_email_login_code,
)
from .schemas import (
    EmailCodeRequest,
    EmailPasswordLoginRequest,
    EmailPasswordResetCompleteRequest,
    EmailPasswordResetVerifyRequest,
    EmailPasswordSetRequest,
    EmailVerifyRequest,
    UserSession,
)
from .redis_client import delete as redis_delete
from .redis_client import get_int as redis_get_int
from .redis_client import incr_with_ttl, set_ex as redis_set_ex, set_nx_ex
from .services.email_service import send_verification_email

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_COOKIE = "uma_session"
CSRF_COOKIE = "uma_csrf"
CSRF_HEADER = "x-csrf-token"
STATE_COOKIE = "uma_oauth_state"
STATE_COOKIE_PATH = "/auth/discord"
EMAIL_SENT_MESSAGE = "如果邮箱地址可用，验证码已经发送。"
EMAIL_REGISTER_PURPOSE = "email_register"
EMAIL_LOGIN_PURPOSE = "email_login"
EMAIL_PASSWORD_RESET_PURPOSE = "email_password_reset"
EMAIL_BUSY_MESSAGE = "邮件服务繁忙，请稍后再试。"
PASSWORD_GENERIC_ERROR = "邮箱或密码错误，或该邮箱尚未设置密码。"
PASSWORD_LOCKED_ERROR = "尝试次数过多，请稍后再试，或使用验证码登录。"
PASSWORD_RULE_ERROR = "密码至少 8 位，并包含字母、数字或符号中的至少两类。"
_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16, type=Type.ID)
_dummy_password_hash = _password_hasher.hash("uma-dummy-password-for-timing")
_email_send_semaphore: asyncio.Semaphore | None = None
_email_send_semaphore_limit = 0


def _email_send_limit(settings: Settings) -> int:
    return max(1, int(settings.email_send_max_concurrency or 2))


def _email_send_timeout(settings: Settings) -> int:
    return max(5, int(settings.email_send_timeout_seconds or 20))


def _email_identity_log_key(settings: Settings, email: str) -> str:
    try:
        return email_identity_digest(settings, email)[:10]
    except Exception:
        return hashlib.sha256(email.strip().lower().encode("utf-8", errors="ignore")).hexdigest()[:10]


def _email_send_semaphore_for(settings: Settings) -> asyncio.Semaphore:
    global _email_send_semaphore, _email_send_semaphore_limit
    limit = _email_send_limit(settings)
    if _email_send_semaphore is None or _email_send_semaphore_limit != limit:
        _email_send_semaphore = asyncio.Semaphore(limit)
        _email_send_semaphore_limit = limit
    return _email_send_semaphore


async def _acquire_email_send_slot(settings: Settings, *, purpose: str, email_key: str) -> asyncio.Semaphore:
    sem = _email_send_semaphore_for(settings)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=0.05)
    except asyncio.TimeoutError as exc:
        redis_set_ex(settings, "uma:email:send:busy", "1", 30)
        print(f"[AUTH] email_send rejected reason=busy purpose={purpose} email_hash={email_key}", flush=True)
        raise HTTPException(status_code=503, detail=EMAIL_BUSY_MESSAGE) from exc
    return sem


def _check_redis_email_send_limits(settings: Settings, *, purpose: str, email_key: str, ip_hash: str | None) -> None:
    cooldown_key = f"uma:email:send:cooldown:{purpose}:{email_key}"
    cooldown = set_nx_ex(settings, cooldown_key, "1", max(1, int(settings.email_otp_resend_seconds)))
    if cooldown is False:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    ten_min = incr_with_ttl(settings, f"uma:email:send:email10m:{purpose}:{email_key}", 600)
    if ten_min is not None and ten_min > int(settings.email_otp_max_sends_per_10_min):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    hour = incr_with_ttl(settings, f"uma:email:send:email1h:{purpose}:{email_key}", 3600)
    if hour is not None and hour > int(settings.email_otp_max_per_hour):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    if ip_hash:
        ip_hour = incr_with_ttl(settings, f"uma:email:send:ip1h:{purpose}:{ip_hash}", 3600)
        if ip_hour is not None and ip_hour > int(settings.email_otp_max_per_ip_per_hour):
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    incr_with_ttl(settings, "uma:email:send:global60s", 60)


def _redis_password_failure_keys(email_hmac: str | None, ip_hash: str | None) -> list[str]:
    keys = []
    if email_hmac:
        keys.append(f"uma:password:fail:email15m:{email_hmac}")
    if ip_hash:
        keys.append(f"uma:password:fail:ip15m:{ip_hash}")
    return keys


def _redis_password_login_rate_check(settings: Settings, *, email_hmac: str | None, ip_hash: str | None) -> None:
    for key in _redis_password_failure_keys(email_hmac, ip_hash):
        count = redis_get_int(settings, key)
        if count is None:
            continue
        if key.startswith("uma:password:fail:email15m:") and count >= int(settings.password_login_max_attempts_per_email_15_min):
            raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)
        if key.startswith("uma:password:fail:ip15m:") and count >= int(settings.password_login_max_attempts_per_ip_15_min):
            raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)


def _record_redis_password_failure(settings: Settings, *, email_hmac: str | None, ip_hash: str | None) -> None:
    for key in _redis_password_failure_keys(email_hmac, ip_hash):
        incr_with_ttl(settings, key, 900)


def _clear_redis_password_failures(settings: Settings, *, email_hmac: str | None, ip_hash: str | None) -> None:
    redis_delete(settings, *_redis_password_failure_keys(email_hmac, ip_hash))


async def _send_verification_email_bounded(settings: Settings, email: str, code: str, *, purpose: str) -> None:
    email_key = _email_identity_log_key(settings, email)
    sem = await _acquire_email_send_slot(settings, purpose=purpose, email_key=email_key)
    start = time.perf_counter()
    try:
        print(f"[AUTH] email_send start purpose={purpose} email_hash={email_key}", flush=True)
        ok = await asyncio.wait_for(
            asyncio.to_thread(send_verification_email, settings, email, code),
            timeout=_email_send_timeout(settings),
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if ok:
            print(f"[AUTH] email_send success purpose={purpose} email_hash={email_key} elapsed_ms={elapsed_ms}", flush=True)
            return
        print(f"[AUTH] email_send failed purpose={purpose} email_hash={email_key} elapsed_ms={elapsed_ms}", flush=True)
        raise HTTPException(status_code=503, detail="暂时无法发送验证码，请稍后重试")
    except asyncio.TimeoutError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        print(f"[AUTH] email_send timeout purpose={purpose} email_hash={email_key} elapsed_ms={elapsed_ms}", flush=True)
        raise HTTPException(status_code=503, detail=EMAIL_BUSY_MESSAGE) from exc
    finally:
        sem.release()


def _cookie_secure(settings: Settings) -> bool:
    return bool(settings.cookie_secure)


def _session_max_age(settings: Settings) -> int:
    return int(settings.session_max_age_seconds)


def _oauth_nonce_cookie_name(state: str | None) -> str | None:
    if not state or "." not in state:
        return None
    handle = state.split(".", 1)[0]
    if not handle or any(ch not in "0123456789abcdef" for ch in handle):
        return None
    return f"uma_oauth_nonce_{handle}"


def _delete_oauth_cookies(response: HTMLResponse | RedirectResponse, settings: Settings, state: str | None = None) -> None:
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH, secure=_cookie_secure(settings), samesite="lax")
    nonce_cookie = _oauth_nonce_cookie_name(state)
    if nonce_cookie:
        response.delete_cookie(nonce_cookie, path=STATE_COOKIE_PATH, secure=_cookie_secure(settings), samesite="lax")


def _oauth_error_response(settings: Settings, state: str | None = None) -> HTMLResponse:
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Discord 登录失败 - 小击击生图</title>
  <link rel="stylesheet" href="/assets/styles.css?v=email-auth">
</head>
<body class="login-body">
  <main class="login-container">
    <div class="login-card">
      <div class="login-header">
        <h1>Discord 登录已过期</h1>
        <p class="muted">Discord 登录已过期或状态不匹配，请重新登录。</p>
      </div>
      <a class="button discord-btn" href="/auth/discord/login">重新登录</a>
    </div>
  </main>
</body>
</html>"""
    response = HTMLResponse(html, status_code=400)
    _delete_oauth_cookies(response, settings, state)
    return response


def _discord_avatar_url(discord_id: str, avatar_hash: str | None) -> str | None:
    if not avatar_hash:
        return None
    ext = "gif" if avatar_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.{ext}?size=128"


def _token_hash(settings: Settings, purpose: str, token: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        f"{purpose}:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _session_token_hash(settings: Settings, session_token: str) -> str:
    return _token_hash(settings, "session", session_token)


def get_session_public_id(settings: Settings, session_token: str | None) -> str | None:
    if not session_token:
        return None
    conn = connect(settings)
    try:
        row = get_active_account_session(conn, session_id_hash=_session_token_hash(settings, session_token))
        return row.get("session_row_id") if row else None
    finally:
        conn.close()


def _csrf_token_hash(settings: Settings, csrf_token: str) -> str:
    return _token_hash(settings, "csrf", csrf_token)


def _password_reset_token_hash(settings: Settings, token: str) -> str:
    return _token_hash(settings, "password-reset", token)


def _validate_password_strength(password: str, confirm_password: str | None = None) -> None:
    if confirm_password is not None and password != confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致。")
    if len(password) < 8 or len(password) > 128 or not password.strip():
        raise HTTPException(status_code=400, detail=PASSWORD_RULE_ERROR)
    lowered = password.strip().lower()
    if lowered in {"12345678", "password", "qwerty123", "11111111", "123456789", "password123"}:
        raise HTTPException(status_code=400, detail=PASSWORD_RULE_ERROR)
    classes = 0
    classes += 1 if any(ch.isalpha() for ch in password) else 0
    classes += 1 if any(ch.isdigit() for ch in password) else 0
    classes += 1 if any(not ch.isalnum() and not ch.isspace() for ch in password) else 0
    if classes < 2:
        raise HTTPException(status_code=400, detail=PASSWORD_RULE_ERROR)


def _hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def _verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash or _dummy_password_hash, password)
    except (VerifyMismatchError, VerificationError, ValueError, TypeError):
        return False


def _password_login_rate_check(conn, settings: Settings, *, email_hmac: str | None, ip_hash: str | None) -> None:
    if email_hmac and count_recent_password_attempts(
        conn,
        email_hmac=email_hmac,
        window_seconds=900,
        success=False,
    ) >= settings.password_login_max_attempts_per_email_15_min:
        raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)
    if ip_hash and count_recent_password_attempts(
        conn,
        ip_hash=ip_hash,
        window_seconds=900,
        success=False,
    ) >= settings.password_login_max_attempts_per_ip_15_min:
        raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)


def _revoke_other_account_sessions(conn, settings: Settings, *, account_id: str, session_token: str | None) -> None:
    if not session_token:
        return
    now = int(time.time())
    conn.execute(
        """
        UPDATE account_sessions
        SET revoked_at=?
        WHERE account_id=?
          AND session_id_hash<>?
          AND revoked_at IS NULL
        """,
        (now, account_id, _session_token_hash(settings, session_token)),
    )


def _session_from_account(account: dict) -> UserSession:
    provider = account["provider"]
    provider_user_id = account["provider_user_id"]
    display_name = account.get("display_username") or account.get("display_name") or provider_user_id
    return UserSession(
        user_id=account["id"],
        username=display_name,
        avatar=account.get("avatar_url") if provider == "discord" else None,
        provider=provider,
        discord_user_id=provider_user_id if provider == "discord" else None,
        is_dev=False,
    )


def _decode_session(token: str, settings: Settings) -> UserSession:
    session_hash = _session_token_hash(settings, token)
    conn = connect(settings)
    try:
        row = get_active_account_session(conn, session_id_hash=session_hash)
        if row:
            touch_account_session(conn, session_id_hash=session_hash)
            conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="登录已失效")
    account = {
        "id": row["account_id"],
        "provider": row["provider"],
        "provider_user_id": row["provider_user_id"],
        "display_name": row["display_name"],
        "display_username": row["display_username"],
        "avatar_url": row["avatar_url"],
        "avatar_hash": row["avatar_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }
    return _session_from_account(account)


def _csrf_error() -> HTTPException:
    return HTTPException(status_code=403, detail="页面安全验证已过期，请刷新后重试。")


def _valid_csrf_token(settings: Settings, session_token: str | None, token: str | None) -> bool:
    if not session_token or not token:
        return False
    conn = connect(settings)
    try:
        return validate_session_csrf(
            conn,
            session_id_hash=_session_token_hash(settings, session_token),
            csrf_token_hash=_csrf_token_hash(settings, token),
        )
    finally:
        conn.close()


def set_csrf_cookie(response: Response, session_token: str, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    conn = connect(settings)
    try:
        conn.execute(
            """
            UPDATE account_sessions
            SET csrf_token_hash=?
            WHERE session_id_hash=? AND revoked_at IS NULL AND expires_at>?
            """,
            (
                _csrf_token_hash(settings, token),
                _session_token_hash(settings, session_token),
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=_session_max_age(settings),
        httponly=False,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    return token


def ensure_csrf_cookie(
    response: Response,
    settings: Settings,
    session_token: str | None,
    existing_token: str | None = None,
) -> str | None:
    if settings.dev_auth_bypass:
        return None
    if not session_token:
        return None
    if _valid_csrf_token(settings, session_token, existing_token):
        return existing_token
    return set_csrf_cookie(response, session_token, settings)


def _delete_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(CSRF_COOKIE, path="/", secure=_cookie_secure(settings), samesite="lax")


async def require_csrf(
    request: Request,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
) -> None:
    if settings.dev_auth_bypass:
        return
    _require_same_origin(request, settings)
    header_token = request.headers.get(CSRF_HEADER)
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not header_token or not cookie_token:
        raise _csrf_error()
    if not hmac.compare_digest(header_token, cookie_token):
        raise _csrf_error()
    if not _valid_csrf_token(settings, session, header_token):
        raise _csrf_error()


def _user_agent_hash(request: Request) -> str:
    user_agent = request.headers.get("user-agent") or "unknown"
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:24]


def _set_session_cookie(response: Response, user: UserSession, settings: Settings, request: Request) -> None:
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        create_account_session(
            conn,
            account_id=user.user_id,
            session_id_hash=_session_token_hash(settings, session_token),
            csrf_token_hash=_csrf_token_hash(settings, csrf_token),
            provider=user.provider,
            expires_at=now + _session_max_age(settings),
            user_agent_hash=_user_agent_hash(request),
            ip_hash=_ip_hash(request),
            max_active_sessions=settings.max_active_sessions_per_account,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=_session_max_age(settings),
        httponly=True,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=_session_max_age(settings),
        httponly=False,
        secure=_cookie_secure(settings),
        samesite="lax",
        path="/",
    )


def _require_same_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin and origin.rstrip("/") != settings.app_origin:
        raise HTTPException(status_code=403, detail="请求来源无效")
    if not origin and referer and not referer.startswith(settings.app_origin + "/"):
        raise HTTPException(status_code=403, detail="请求来源无效")


def _ip_hash(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    host = cf_ip.strip() if cf_ip else (request.client.host if request.client else "unknown")
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:24]


async def get_current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
) -> UserSession:
    if settings.dev_auth_bypass:
        return UserSession(
            user_id="dev-local-account",
            username=settings.dev_username,
            provider="dev",
            discord_user_id=settings.dev_user_id,
            is_dev=True,
        )
    if not session:
        raise HTTPException(status_code=401, detail="请先登录")
    return _decode_session(session, settings)


async def get_current_user_optional(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
) -> UserSession | None:
    if settings.dev_auth_bypass:
        return UserSession(
            user_id="dev-local-account",
            username=settings.dev_username,
            provider="dev",
            discord_user_id=settings.dev_user_id,
            is_dev=True,
        )
    if not session:
        return None
    try:
        return _decode_session(session, settings)
    except HTTPException:
        return None


def get_legacy_user_id_for_session(user: UserSession, settings: Settings) -> str:
    if user.is_dev:
        return settings.dev_user_id
    conn = connect(settings)
    try:
        legacy_id = get_account_legacy_user_id(conn, user.user_id)
        if legacy_id:
            return legacy_id
        if user.discord_user_id:
            bind_account_legacy_user(conn, user.user_id, user.discord_user_id)
            conn.commit()
            return user.discord_user_id
    finally:
        conn.close()
    raise HTTPException(status_code=401, detail="账户映射无效")


def is_admin_user(user: UserSession, settings: Settings) -> bool:
    return bool(user.discord_user_id) and user.discord_user_id == settings.admin_discord_user_id


@router.get("/discord/login")
async def discord_login(request: Request, settings: Settings = Depends(get_settings)):
    if settings.dev_auth_bypass:
        return RedirectResponse(url="/", status_code=302)
    handle = secrets.token_hex(12)
    state = f"{handle}.{secrets.token_urlsafe(32)}"
    browser_nonce = secrets.token_urlsafe(32)
    redirect_after_login = request.query_params.get("next") or "/"
    if not redirect_after_login.startswith("/") or redirect_after_login.startswith("//"):
        redirect_after_login = "/"
    conn = connect(settings)
    try:
        cleanup_oauth_login_states(conn)
        create_oauth_login_state(
            conn,
            state=state,
            browser_nonce=browser_nonce,
            provider="discord",
            redirect_after_login=redirect_after_login,
            ttl_seconds=600,
        )
        conn.commit()
    finally:
        conn.close()
    params = urlencode({
        "client_id": settings.discord_client_id,
        "redirect_uri": settings.discord_redirect_uri,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    })
    response = RedirectResponse(url=f"https://discord.com/oauth2/authorize?{params}", status_code=302)
    nonce_cookie = _oauth_nonce_cookie_name(state)
    if nonce_cookie:
        response.set_cookie(
            nonce_cookie,
            browser_nonce,
            max_age=600,
            httponly=True,
            secure=_cookie_secure(settings),
            samesite="lax",
            path=STATE_COOKIE_PATH,
        )
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH, secure=_cookie_secure(settings), samesite="lax")
    return response


@router.get("/login")
async def legacy_discord_login(request: Request, settings: Settings = Depends(get_settings)):
    return await discord_login(request, settings)


@router.get("/discord/callback")
async def discord_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    settings: Settings = Depends(get_settings),
):
    if settings.dev_auth_bypass:
        return RedirectResponse(url="/", status_code=302)
    nonce_cookie = _oauth_nonce_cookie_name(state)
    browser_nonce = request.cookies.get(nonce_cookie) if nonce_cookie else None
    if not code or not state or not browser_nonce:
        return _oauth_error_response(settings, state)

    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        redirect_after_login = consume_oauth_login_state(
            conn,
            state=state,
            browser_nonce=browser_nonce,
            provider="discord",
        )
        if not redirect_after_login:
            conn.rollback()
            return _oauth_error_response(settings, state)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": settings.discord_client_id,
                    "client_secret": settings.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.discord_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_response.status_code != 200:
                return _oauth_error_response(settings, state)
            access_token = token_response.json().get("access_token")
            if not access_token:
                return _oauth_error_response(settings, state)

            user_response = await client.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_response.status_code != 200:
                return _oauth_error_response(settings, state)
            data = user_response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Discord 服务暂时不可用，请稍后重试") from exc

    discord_id = str(data.get("id") or "")
    if not discord_id.isdigit():
        return _oauth_error_response(settings, state)
    display_name = str(data.get("global_name") or data.get("username") or discord_id)
    avatar_hash = data.get("avatar")
    avatar_url = _discord_avatar_url(discord_id, avatar_hash)

    conn = connect(settings)
    try:
        account = find_or_create_discord_account(conn, discord_id, display_name, avatar_hash, avatar_url)
        bind_account_legacy_user(conn, account["id"], discord_id)
        if account.get("is_new"):
            grant_welcome_bonus_if_needed(conn, account["id"], legacy_user_id=discord_id)
        conn.commit()
    finally:
        conn.close()

    user = _session_from_account(account)
    response = RedirectResponse(url=redirect_after_login, status_code=302)
    _delete_oauth_cookies(response, settings, state)
    _set_session_cookie(response, user, settings, request)
    return response

async def _email_send_code_for_purpose(
    body: EmailCodeRequest,
    request: Request,
    purpose: str,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
        email_key = _email_identity_log_key(settings, body.email)
        ip_digest = _ip_hash(request)
        _check_redis_email_send_limits(settings, purpose=purpose, email_key=email_key, ip_hash=ip_digest)
        code = create_secure_email_code(settings, body.email, purpose, ip_digest)
        if not code:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
        await _send_verification_email_bounded(settings, body.email, code, purpose=purpose)
    except ValueError:
        pass
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="暂时无法发送验证码，请稍后重试") from exc
    return {"ok": True, "message": EMAIL_SENT_MESSAGE}


async def _email_login_verify_code(
    body: EmailVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="验证码无效或已过期") from exc
    if not verify_secure_email_code(settings, body.email, body.code, EMAIL_LOGIN_PURPOSE):
        raise HTTPException(status_code=400, detail="验证码无效或已过期")

    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        account = get_email_account_by_hmac(conn, settings, body.email)
        if not account:
            conn.rollback()
            raise HTTPException(status_code=404, detail="该邮箱尚未注册，请先注册")
        upsert_account_email_identity(conn, settings, account["id"], body.email)
        clear_password_failures(conn, account["id"])
        mark_account_login(conn, account["id"])
        account["display_name"] = account["display_name"] or "邮箱用户"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    user = _session_from_account(account)
    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookie(response, user, settings, request)
    return response


async def _email_register_verify_code(
    body: EmailVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="验证码无效或已过期") from exc
    if not verify_secure_email_code(settings, body.email, body.code, EMAIL_REGISTER_PURPOSE):
        raise HTTPException(status_code=400, detail="验证码无效或已过期")

    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if get_email_account_by_hmac(conn, settings, body.email):
            conn.rollback()
            raise HTTPException(status_code=409, detail="该邮箱已经注册，请使用邮箱登录。")
        account = create_email_account(conn, settings, body.email, invite_code=body.invite_code)
        conn.commit()
    except HTTPException:
        raise
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    user = _session_from_account(account)
    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookie(response, user, settings, request)
    return response


@router.post("/email/login/send-code")
async def email_login_send_code(
    body: EmailCodeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_send_code_for_purpose(body, request, EMAIL_LOGIN_PURPOSE, settings)


@router.post("/email/login/verify-code")
async def email_login_verify_code(
    body: EmailVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_login_verify_code(body, request, settings)


@router.post("/email/register/send-code")
async def email_register_send_code(
    body: EmailCodeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_send_code_for_purpose(body, request, EMAIL_REGISTER_PURPOSE, settings)


@router.post("/email/register/verify-code")
async def email_register_verify_code(
    body: EmailVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_register_verify_code(body, request, settings)


@router.post("/email/send-code")
async def email_send_code(
    body: EmailCodeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_send_code_for_purpose(body, request, EMAIL_LOGIN_PURPOSE, settings)


@router.post("/email/verify-code")
async def email_verify_code(
    body: EmailVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    return await _email_login_verify_code(body, request, settings)


@router.get("/email/password/status")
async def email_password_status(
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    if user.provider != "email":
        return {"ok": True, "provider": user.provider, "available": False, "has_password": False}
    conn = connect(settings)
    try:
        has_password = email_account_has_password(conn, user.user_id)
    finally:
        conn.close()
    return {"ok": True, "provider": "email", "available": True, "has_password": has_password}


@router.post("/email/password/set")
async def email_password_set(
    body: EmailPasswordSetRequest,
    request: Request,
    csrf: None = Depends(require_csrf),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    if user.provider != "email":
        raise HTTPException(status_code=404, detail="Not found")
    _validate_password_strength(body.password, body.confirm_password)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        account = get_account_by_id(conn, user.user_id)
        if not account or account["provider"] != "email":
            conn.rollback()
            raise HTTPException(status_code=404, detail="Not found")
        credential = get_email_password_credential(conn, user.user_id)
        now = int(time.time())
        if credential:
            locked_until = int(credential["locked_until"] or 0)
            if locked_until and locked_until > now:
                conn.rollback()
                raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)
            if not body.old_password:
                conn.rollback()
                raise HTTPException(status_code=400, detail="请输入当前密码。")
            if not _verify_password(credential["password_hash"], body.old_password):
                register_password_failure(
                    conn,
                    account_id=user.user_id,
                    max_failed_attempts=settings.password_login_max_failed_attempts,
                    lock_seconds=settings.password_login_lock_seconds,
                    now=now,
                )
                conn.commit()
                raise HTTPException(status_code=400, detail="当前密码错误。")
        upsert_email_password_credential(conn, user.user_id, _hash_password(body.password), "argon2id", now=now)
        if body.revoke_other_sessions:
            _revoke_other_account_sessions(conn, settings, account_id=user.user_id, session_token=session)
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"[AUTH] password_set provider=email account={user.user_id[:8]} revoke_other={bool(body.revoke_other_sessions)}", flush=True)
    return {"ok": True, "message": "登录密码已保存。"}


@router.post("/email/password/login")
async def email_password_login(
    body: EmailPasswordLoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
        email_hmac = email_identity_digest(settings, body.email)
    except ValueError:
        email_hmac = None
    ip_digest = _ip_hash(request)
    _redis_password_login_rate_check(settings, email_hmac=email_hmac, ip_hash=ip_digest)
    conn = connect(settings)
    account = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        _password_login_rate_check(conn, settings, email_hmac=email_hmac, ip_hash=ip_digest)
        if email_hmac:
            account = get_email_account_by_hmac(conn, settings, body.email)
        credential = get_email_password_credential(conn, account["id"]) if account else None
        now = int(time.time())
        if credential and int(credential["locked_until"] or 0) > now:
            _record_redis_password_failure(settings, email_hmac=email_hmac, ip_hash=ip_digest)
            record_email_password_attempt(conn, email_hmac=email_hmac, ip_hash=ip_digest, success=False, now=now)
            conn.commit()
            raise HTTPException(status_code=429, detail=PASSWORD_LOCKED_ERROR)
        ok = bool(credential) and _verify_password(credential["password_hash"], body.password)
        if not ok:
            _verify_password(None, body.password)
            _record_redis_password_failure(settings, email_hmac=email_hmac, ip_hash=ip_digest)
            record_email_password_attempt(conn, email_hmac=email_hmac, ip_hash=ip_digest, success=False, now=now)
            if account and credential:
                register_password_failure(
                    conn,
                    account_id=account["id"],
                    max_failed_attempts=settings.password_login_max_failed_attempts,
                    lock_seconds=settings.password_login_lock_seconds,
                    now=now,
                )
            conn.commit()
            if email_hmac:
                print(f"[AUTH] password_login failed reason=bad_password account_hash={email_hmac[:8]}", flush=True)
            raise HTTPException(status_code=400, detail=PASSWORD_GENERIC_ERROR)
        clear_password_failures(conn, account["id"], now=now)
        _clear_redis_password_failures(settings, email_hmac=email_hmac, ip_hash=ip_digest)
        record_email_password_attempt(conn, email_hmac=email_hmac, ip_hash=ip_digest, success=True, now=now)
        upsert_account_email_identity(conn, settings, account["id"], body.email)
        mark_account_login(conn, account["id"])
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    user = _session_from_account(account)
    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookie(response, user, settings, request)
    print(f"[AUTH] password_login success account={account['id'][:8]}", flush=True)
    return response


@router.post("/email/password/reset/send-code")
async def email_password_reset_send_code(
    body: EmailCodeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
        email_key = _email_identity_log_key(settings, body.email)
        ip_digest = _ip_hash(request)
        _check_redis_email_send_limits(settings, purpose=EMAIL_PASSWORD_RESET_PURPOSE, email_key=email_key, ip_hash=ip_digest)
        conn = connect(settings)
        try:
            account = get_email_account_by_hmac(conn, settings, body.email)
        finally:
            conn.close()
        if account:
            code = create_secure_email_code(settings, body.email, EMAIL_PASSWORD_RESET_PURPOSE, ip_digest)
            if not code:
                raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
            await _send_verification_email_bounded(settings, body.email, code, purpose=EMAIL_PASSWORD_RESET_PURPOSE)
            print(f"[AUTH] password_reset_requested account={account['id'][:8]}", flush=True)
    except ValueError:
        pass
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="暂时无法发送验证码，请稍后重试") from exc
    return {"ok": True, "message": "如果邮箱已注册，验证码已经发送。"}


@router.post("/email/password/reset/verify-code")
async def email_password_reset_verify_code(
    body: EmailPasswordResetVerifyRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    try:
        normalize_email_for_identity(body.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="验证码无效或已过期") from exc
    if not verify_secure_email_code(settings, body.email, body.code, EMAIL_PASSWORD_RESET_PURPOSE):
        raise HTTPException(status_code=400, detail="验证码无效或已过期")
    reset_token = secrets.token_urlsafe(40)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        account = get_email_account_by_hmac(conn, settings, body.email)
        if not account:
            conn.rollback()
            raise HTTPException(status_code=404, detail="该邮箱尚未注册。")
        create_password_reset_token(
            conn,
            settings=settings,
            account_id=account["id"],
            email=body.email,
            token_hash=_password_reset_token_hash(settings, reset_token),
            requested_ip_hash=_ip_hash(request),
            ttl_seconds=settings.password_reset_token_seconds,
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "reset_token": reset_token}


@router.post("/email/password/reset/complete")
async def email_password_reset_complete(
    body: EmailPasswordResetCompleteRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    _require_same_origin(request, settings)
    if not settings.is_email_auth_available():
        raise HTTPException(status_code=503, detail="邮箱登录未启用")
    _validate_password_strength(body.password, body.confirm_password)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        token_row = consume_password_reset_token(
            conn,
            token_hash=_password_reset_token_hash(settings, body.reset_token),
        )
        if not token_row:
            conn.rollback()
            raise HTTPException(status_code=400, detail="重置链接已失效，请重新获取验证码。")
        account = get_account_by_id(conn, token_row["account_id"])
        if not account or account["provider"] != "email":
            conn.rollback()
            raise HTTPException(status_code=404, detail="该邮箱尚未注册。")
        now = int(time.time())
        upsert_email_password_credential(conn, account["id"], _hash_password(body.password), "argon2id", now=now)
        revoke_all_account_sessions(conn, account_id=account["id"], now=now)
        mark_account_login(conn, account["id"])
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    user = _session_from_account(account)
    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookie(response, user, settings, request)
    print(f"[AUTH] password_reset_completed account={account['id'][:8]}", flush=True)
    return response


@router.post("/logout")
async def logout(
    csrf: None = Depends(require_csrf),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
):
    response = RedirectResponse(url="/login", status_code=303)
    if session:
        conn = connect(settings)
        try:
            revoke_account_session(conn, session_id_hash=_session_token_hash(settings, session))
            conn.commit()
        finally:
            conn.close()
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(settings),
        samesite="lax",
    )
    _delete_csrf_cookie(response, settings)
    return response


@router.post("/logout-all")
async def logout_all(
    csrf: None = Depends(require_csrf),
    user: UserSession = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    conn = connect(settings)
    try:
        revoke_all_account_sessions(conn, account_id=user.user_id)
        conn.commit()
    finally:
        conn.close()
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(settings),
        samesite="lax",
    )
    _delete_csrf_cookie(response, settings)
    return response
