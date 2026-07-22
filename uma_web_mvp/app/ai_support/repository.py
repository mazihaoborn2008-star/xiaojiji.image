from __future__ import annotations

import secrets
import string
import time
from typing import Any

from app.config import Settings
from app.db import connect


def make_conversation_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "AIS-" + "".join(secrets.choice(alphabet) for _ in range(12))


def create_conversation(settings: Settings, user_id: str, title: str = "") -> dict[str, Any]:
    now = int(time.time())
    code = make_conversation_code()
    conn = connect(settings)
    try:
        cur = conn.execute(
            """
            INSERT INTO ai_support_conversations(conversation_code,user_id,title,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            (code, user_id, title[:120], "open", now, now),
        )
        conversation_id = int(cur.lastrowid)
        conn.commit()
        return {"id": conversation_id, "conversation_code": code, "title": title[:120], "created_at": now, "updated_at": now}
    finally:
        conn.close()


def list_conversations(settings: Settings, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT id, conversation_code, title, status, created_at, updated_at
            FROM ai_support_conversations
            WHERE user_id=?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, max(1, min(int(limit), 50))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_conversation(settings: Settings, user_id: str, conversation_code: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT id, conversation_code, title, status, created_at, updated_at
            FROM ai_support_conversations
            WHERE user_id=? AND conversation_code=?
            """,
            (user_id, conversation_code),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_messages(settings: Settings, conversation_id: int, limit: int = 50) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT id, role, safe_content, created_at, status, referenced_job_code
            FROM ai_support_messages
            WHERE conversation_id=?
            ORDER BY id ASC
            LIMIT ?
            """,
            (conversation_id, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_recent_history(settings: Settings, conversation_id: int, limit: int) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT role, safe_content, referenced_job_code
            FROM ai_support_messages
            WHERE conversation_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, max(1, min(int(limit), 50))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def add_message(settings: Settings, conversation_id: int, role: str, safe_content: str, *, status: str = "done", referenced_job_code: str = "") -> dict[str, Any]:
    now = int(time.time())
    conn = connect(settings)
    try:
        cur = conn.execute(
            """
            INSERT INTO ai_support_messages(conversation_id,role,safe_content,created_at,status,referenced_job_code)
            VALUES (?,?,?,?,?,?)
            """,
            (conversation_id, role, safe_content, now, status, referenced_job_code[:32]),
        )
        conn.execute("UPDATE ai_support_conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        conn.commit()
        return {
            "id": int(cur.lastrowid),
            "role": role,
            "safe_content": safe_content,
            "created_at": now,
            "status": status,
            "referenced_job_code": referenced_job_code[:32],
        }
    finally:
        conn.close()


def clear_conversation(settings: Settings, user_id: str, conversation_code: str) -> bool:
    conversation = get_conversation(settings, user_id, conversation_code)
    if not conversation:
        return False
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("DELETE FROM ai_support_messages WHERE conversation_id=?", (int(conversation["id"]),))
        conn.execute("UPDATE ai_support_conversations SET updated_at=? WHERE id=?", (now, int(conversation["id"])))
        conn.commit()
        return True
    finally:
        conn.close()

