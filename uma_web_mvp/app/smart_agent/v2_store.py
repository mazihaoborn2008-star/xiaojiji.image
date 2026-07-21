from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.config import Settings
from app.db import connect


_ACTIVE_TURN_STATUSES = ("accepted", "processing")
_TERMINAL_TURN_STATUSES = {"completed", "failed", "cancelled", "awaiting_user"}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS smart_agent_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL UNIQUE,
            conversation_id INTEGER NOT NULL,
            client_request_id TEXT NOT NULL DEFAULT '',
            message_id INTEGER,
            generation_requested INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_smart_agent_turns_conversation
            ON smart_agent_turns(conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_smart_agent_turns_message
            ON smart_agent_turns(message_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_smart_agent_turns_client_request
            ON smart_agent_turns(conversation_id, client_request_id)
            WHERE client_request_id != '';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_smart_agent_turns_one_active
            ON smart_agent_turns(conversation_id)
            WHERE status IN ('accepted', 'processing');

        CREATE TABLE IF NOT EXISTS smart_agent_message_resolution (
            message_id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            character_ids_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_smart_agent_resolution_conversation
            ON smart_agent_message_resolution(conversation_id, created_at);
        """
    )




def _recover_stale_turns(conn: sqlite3.Connection, settings: Settings, now: int) -> None:
    timeout = max(30, int(getattr(settings, "deepseek_chat_timeout_seconds", 180) or 180))
    retries = max(0, int(getattr(settings, "deepseek_max_retries", 2) or 0))
    stale_seconds = max(900, timeout * (retries + 1) + 300)
    stale_before = int(now) - stale_seconds
    conn.execute(
        "UPDATE smart_agent_turns SET status='failed', error='recovered_stale_turn', "
        "updated_at=?, finished_at=? WHERE status IN ('accepted','processing') AND updated_at < ?",
        (int(now), int(now), stale_before),
    )

def ensure_v2_schema(settings: Settings) -> None:
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()


def begin_turn_atomic(
    settings: Settings,
    *,
    conversation_id: int,
    client_request_id: str,
    generation_requested: bool,
    turn_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    request_id = str(client_request_id or "").strip()[:100]
    normalized_turn_id = str(turn_id or uuid.uuid4().hex).strip()[:80]
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale_turns(conn, settings, now)

        if request_id:
            existing = conn.execute(
                "SELECT * FROM smart_agent_turns WHERE conversation_id=? AND client_request_id=?",
                (int(conversation_id), request_id),
            ).fetchone()
            if existing:
                conn.commit()
                item = dict(existing)
                item["duplicate"] = True
                return item

        active = conn.execute(
            "SELECT turn_id, message_id, status FROM smart_agent_turns "
            "WHERE conversation_id=? AND status IN ('accepted','processing') "
            "ORDER BY id DESC LIMIT 1",
            (int(conversation_id),),
        ).fetchone()
        if active:
            raise RuntimeError("smart_agent_turn_in_progress")

        # Also respect old pending/processing messages created before V2 was
        # enabled. This prevents a rollout from allowing two simultaneous turns.
        legacy_busy = conn.execute(
            "SELECT id FROM smart_agent_messages WHERE conversation_id=? AND role='user' "
            "AND status IN ('pending','processing') ORDER BY id DESC LIMIT 1",
            (int(conversation_id),),
        ).fetchone()
        if legacy_busy:
            raise RuntimeError("smart_agent_turn_in_progress")

        conn.execute(
            "INSERT INTO smart_agent_turns(" 
            "turn_id,conversation_id,client_request_id,generation_requested,status,created_at,updated_at" 
            ") VALUES (?,?,?,?,?,?,?)",
            (
                normalized_turn_id,
                int(conversation_id),
                request_id,
                1 if generation_requested else 0,
                "accepted",
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM smart_agent_turns WHERE turn_id=?",
            (normalized_turn_id,),
        ).fetchone()
        conn.commit()
        item = dict(row)
        item["duplicate"] = False
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bind_turn_message(settings: Settings, *, turn_id: str, message_id: int) -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_turns SET message_id=?, updated_at=? "
            "WHERE turn_id=? AND status='accepted'",
            (int(message_id), now, str(turn_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_turn_processing(settings: Settings, *, message_id: int) -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_turns SET status='processing', updated_at=? "
            "WHERE message_id=? AND status='accepted'",
            (now, int(message_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_turn_for_message(
    settings: Settings,
    *,
    message_id: int,
    status: str = "completed",
    error: str = "",
) -> None:
    normalized = status if status in _TERMINAL_TURN_STATUSES else "completed"
    now = int(time.time())
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_turns SET status=?, error=?, updated_at=?, finished_at=? "
            "WHERE message_id=? AND status IN ('accepted','processing')",
            (normalized, str(error or "")[:500], now, now, int(message_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def abort_turn(settings: Settings, *, turn_id: str, error: str = "") -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_turns SET status='failed', error=?, updated_at=?, finished_at=? "
            "WHERE turn_id=? AND status IN ('accepted','processing')",
            (str(error or "")[:500], now, now, str(turn_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_active_turn(settings: Settings, *, conversation_id: int) -> bool:
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        _recover_stale_turns(conn, settings, now)
        conn.commit()
        row = conn.execute(
            "SELECT 1 FROM smart_agent_turns WHERE conversation_id=? "
            "AND status IN ('accepted','processing') LIMIT 1",
            (int(conversation_id),),
        ).fetchone()
        if row:
            return True
        legacy = conn.execute(
            "SELECT 1 FROM smart_agent_messages WHERE conversation_id=? AND role='user' "
            "AND status IN ('pending','processing') LIMIT 1",
            (int(conversation_id),),
        ).fetchone()
        return bool(legacy)
    finally:
        conn.close()


def save_message_resolution(
    settings: Settings,
    *,
    conversation_id: int,
    message_id: int,
    character_ids: list[str],
    source: str,
) -> None:
    clean_ids: list[str] = []
    seen: set[str] = set()
    for item in character_ids:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        clean_ids.append(key)
    if not clean_ids:
        raise ValueError("empty_character_resolution")
    now = int(time.time())
    payload = json.dumps(clean_ids[:4], ensure_ascii=False, separators=(",", ":"))
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO smart_agent_message_resolution(" 
            "message_id,conversation_id,character_ids_json,source,created_at" 
            ") VALUES (?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET "
            "character_ids_json=excluded.character_ids_json,source=excluded.source,created_at=excluded.created_at",
            (int(message_id), int(conversation_id), payload, str(source or "")[:80], now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_message_resolution(settings: Settings, *, message_id: int) -> list[str]:
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        row = conn.execute(
            "SELECT character_ids_json FROM smart_agent_message_resolution WHERE message_id=?",
            (int(message_id),),
        ).fetchone()
        if not row:
            return []
        try:
            data = json.loads(str(row["character_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return [str(item).strip() for item in data if str(item).strip()] if isinstance(data, list) else []
    finally:
        conn.close()


def clear_v2_state(settings: Settings, *, conversation_id: int) -> None:
    conn = connect(settings)
    try:
        _ensure_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM smart_agent_message_resolution WHERE conversation_id=?",
            (int(conversation_id),),
        )
        conn.execute(
            "DELETE FROM smart_agent_turns WHERE conversation_id=?",
            (int(conversation_id),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
