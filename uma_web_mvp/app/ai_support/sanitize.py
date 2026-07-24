from __future__ import annotations

import re

PATH_RE = re.compile(r"([A-Za-z]:[\\/][^\s`\"'<>]*|/(?:mnt|home|var|etc|tmp|usr)/[^\s`\"'<>]*)")
SECRET_RE = re.compile(
    r"(api[_-]?key|token|cookie|session|csrf|password|secret|authorization)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
KEYLIKE_RE = re.compile(r"\b(?:sk|sess|tok|key)_[A-Za-z0-9_\-]{16,}\b", re.IGNORECASE)
SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|PRAGMA|sqlite_master)\b", re.IGNORECASE)
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", re.IGNORECASE | re.DOTALL)
HTML_RE = re.compile(r"<\s*/?\s*(script|iframe|object|embed|style|link|meta)\b[^>]*>", re.IGNORECASE)
INTERNAL_HOST_RE = re.compile(
    r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|::1|redis://[^\s]+|https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)[^\s]*)\b",
    re.IGNORECASE,
)
INTERNAL_TABLE_RE = re.compile(
    r"\b(?:generation_tasks|translation_requests|balance_ledger|users|accounts|sessions|email_login_codes|ai_support_messages)\b",
    re.IGNORECASE,
)


def sanitize_user_message(text: str, max_length: int = 2000) -> str:
    clean = str(text or "").replace("\x00", "").strip()
    clean = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", clean)
    return clean[:max_length]


def sanitize_ai_reply(text: str, max_length: int = 4000) -> str:
    clean = str(text or "").replace("```", "").replace("<think>", "").replace("</think>", "")
    clean = TRACEBACK_RE.sub("[hidden-traceback]", clean)
    clean = HTML_RE.sub("[hidden-html]", clean)
    clean = PATH_RE.sub("[hidden-path]", clean)
    clean = SECRET_RE.sub(r"\1=[hidden]", clean)
    clean = KEYLIKE_RE.sub("[hidden-key]", clean)
    clean = INTERNAL_HOST_RE.sub("[hidden-host]", clean)
    clean = INTERNAL_TABLE_RE.sub("[internal]", clean)
    clean = SQL_RE.sub("[internal]", clean)
    return clean.strip()[:max_length] or "我暂时无法确认这个问题，请稍后再试。"

