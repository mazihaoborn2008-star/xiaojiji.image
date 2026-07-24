from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


PUBLIC_DEEPSEEK_ERROR_CODES = {
    "deepseek_auth_failed",
    "deepseek_rate_limited",
    "deepseek_timeout",
    "deepseek_invalid_response",
    "deepseek_unavailable",
}

_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_HTTP_STATUS_RE = re.compile(r"(?:^|[^0-9])([1-5][0-9]{2})(?:[^0-9]|$)")
_SECRETISH_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|api[_-]?key|authorization|bearer)", re.IGNORECASE)
_PATH_OR_URL_RE = re.compile(r"(https?://|[A-Za-z]:\\|/mnt/|/home/|/var/|/etc/)")


@dataclass(frozen=True)
class ProviderErrorInfo:
    public_code: str
    internal_code: str
    http_status: int | None = None
    exception_type: str = ""


def sanitize_public_error_code(raw: Any, *, default: str = "deepseek_unavailable") -> str:
    """Return a short public error code, never a provider/raw exception string."""
    code = str(raw or "").strip().lower().replace("-", "_")
    if not code:
        return default
    if code in PUBLIC_DEEPSEEK_ERROR_CODES:
        return code
    if code in {"deepseek_invalid_json", "deepseek_json_not_object", "jsondecodeerror"}:
        return "deepseek_invalid_response"
    if code in {"timeout", "readtimeout", "connecttimeout", "pooltimeout", "deepseek_timeout"}:
        return "deepseek_timeout"
    if "401" in code or "403" in code or "auth" in code or "unauthorized" in code:
        return "deepseek_auth_failed"
    if "429" in code or "rate" in code:
        return "deepseek_rate_limited"
    if "json" in code or "invalid_response" in code or "malformed" in code:
        return "deepseek_invalid_response"
    if _PATH_OR_URL_RE.search(code) or _SECRETISH_RE.search(code):
        return default
    if _SAFE_CODE_RE.fullmatch(code):
        return code[:80]
    return default


def classify_deepseek_failure(exc: BaseException | None) -> ProviderErrorInfo:
    if exc is None:
        return ProviderErrorInfo("deepseek_unavailable", "deepseek_unknown")

    exc_type = type(exc).__name__
    http_status = _extract_http_status(exc)
    public_code = _map_public_deepseek_code(exc, http_status)
    internal_code = public_code
    if http_status is not None:
        internal_code = f"{public_code}:http_{http_status}"
    elif exc_type:
        internal_code = f"{public_code}:{exc_type}"
    return ProviderErrorInfo(
        public_code=public_code,
        internal_code=internal_code[:120],
        http_status=http_status,
        exception_type=exc_type[:80],
    )


def _map_public_deepseek_code(exc: BaseException, http_status: int | None) -> str:
    raw = str(exc or "")
    raw_lower = raw.lower()

    existing_code = getattr(exc, "code", None)
    if existing_code:
        return sanitize_public_error_code(existing_code)

    if http_status in {401, 403}:
        return "deepseek_auth_failed"
    if http_status == 429:
        return "deepseek_rate_limited"
    if http_status is not None and 500 <= http_status <= 599:
        return "deepseek_unavailable"
    if http_status is not None:
        return "deepseek_unavailable"

    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "deepseek_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "deepseek_unavailable"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "deepseek_invalid_response"

    if "deepseek_invalid_json" in raw_lower or "deepseek_json_not_object" in raw_lower:
        return "deepseek_invalid_response"
    if "timeout" in raw_lower or "timed out" in raw_lower:
        return "deepseek_timeout"
    if "401" in raw_lower or "403" in raw_lower or "unauthorized" in raw_lower or "auth" in raw_lower:
        return "deepseek_auth_failed"
    if "429" in raw_lower or "rate limit" in raw_lower:
        return "deepseek_rate_limited"
    if "json" in raw_lower or "invalid response" in raw_lower:
        return "deepseek_invalid_response"
    return "deepseek_unavailable"


def _extract_http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    raw = str(exc or "")
    match = _HTTP_STATUS_RE.search(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None

