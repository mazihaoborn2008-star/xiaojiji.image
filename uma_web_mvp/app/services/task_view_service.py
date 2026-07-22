from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.db import connect

JOB_CODE_RE = re.compile(r"\bGEN-[A-Z0-9]{12}\b", re.IGNORECASE)
NOT_FOUND_MESSAGE = "未找到该任务，或该任务不属于当前账号。"


def extract_job_codes(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in JOB_CODE_RE.findall(str(text or "")):
        code = match.upper()
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


def _public_error(row: Any) -> str:
    code = str(row["error_code"] or "").strip()
    if code:
        return code
    text = str(row["error"] or "").strip()
    if not text:
        return ""
    if "Traceback" in text or ":\\" in text or "/mnt/" in text or "/home/" in text:
        return "internal_error"
    return text[:160]


def _task_summary_from_row(row: Any, refunded: bool = False) -> dict[str, Any]:
    return {
        "job_code": row["job_code"],
        "status": row["status"],
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["finished_at"] or row["started_at"] or row["created_at"] or 0),
        "completed_at": int(row["finished_at"] or 0) if row["finished_at"] else None,
        "charged_credits": int(row["charged_fen"] or 0),
        "refunded": bool(refunded),
        "public_error": _public_error(row),
        "style_name": str(row["style_key"] or ""),
        "mode": str(row["generation_mode"] or "txt2img"),
    }


def get_owned_task_summary(settings: Settings, user_id: str, job_code: str) -> dict[str, Any] | None:
    code = str(job_code or "").strip().upper()
    if not JOB_CODE_RE.fullmatch(code):
        return None
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT job_code,status,created_at,started_at,finished_at,charged_fen,error,error_code,style_key,generation_mode
            FROM generation_tasks
            WHERE user_id=? AND job_code=?
            """,
            (user_id, code),
        ).fetchone()
        if not row:
            return None
        refunded_amount = conn.execute(
            """
            SELECT COALESCE(SUM(amount_fen), 0)
            FROM balance_ledger
            WHERE user_id=? AND order_code=? AND amount_fen > 0
            """,
            (user_id, code),
        ).fetchone()[0]
        return _task_summary_from_row(row, refunded=int(refunded_amount or 0) > 0)
    finally:
        conn.close()


def get_owned_recent_tasks(settings: Settings, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT job_code,status,created_at,started_at,finished_at,charged_fen,error,error_code,style_key,generation_mode
            FROM generation_tasks
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, max(1, min(int(limit), 10))),
        ).fetchall()
        return [_task_summary_from_row(row) for row in rows]
    finally:
        conn.close()


def get_owned_task_charge_status(settings: Settings, user_id: str, job_code: str) -> dict[str, Any] | None:
    summary = get_owned_task_summary(settings, user_id, job_code)
    if not summary:
        return None
    return {
        "job_code": summary["job_code"],
        "charged_credits": summary["charged_credits"],
        "refunded": summary["refunded"],
        "status": summary["status"],
    }


def get_owned_task_refund_status(settings: Settings, user_id: str, job_code: str) -> dict[str, Any] | None:
    summary = get_owned_task_summary(settings, user_id, job_code)
    if not summary:
        return None
    return {
        "job_code": summary["job_code"],
        "refunded": summary["refunded"],
        "status": summary["status"],
        "charged_credits": summary["charged_credits"],
    }
