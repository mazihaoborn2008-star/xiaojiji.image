from __future__ import annotations

import re

PATH_RE = re.compile(r"([A-Za-z]:\\|/mnt/|/home/|/var/|/etc/)")
SECRET_RE = re.compile(
    r"(api[_-]?key|token|cookie|session|csrf|password|secret)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER)\b", re.IGNORECASE)


def sanitize_user_message(text: str, max_length: int = 2000) -> str:
    clean = str(text or "").replace("\x00", "").strip()
    clean = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", clean)
    return clean[:max_length]


def sanitize_ai_reply(text: str, max_length: int = 4000) -> str:
    clean = str(text or "").replace("```", "").replace("<think>", "").replace("</think>", "")
    clean = PATH_RE.sub("[hidden-path]", clean)
    clean = SECRET_RE.sub(r"\1=[hidden]", clean)
    clean = SQL_RE.sub("[internal]", clean)
    return clean.strip()[:max_length] or "我暂时无法确认这个问题，请稍后再试。"

