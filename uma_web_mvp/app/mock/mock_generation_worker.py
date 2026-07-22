from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from app.config import get_settings, Settings
from app.db import connect

MOCK_REFUND_REASON = "mock_generation_refund"


def validate_mock_environment(settings: Settings) -> None:
    settings.validate_local_isolation()
    if not settings.is_local_env():
        raise RuntimeError("LOCAL MOCK WORKER requires APP_ENV=local")
    if not settings.mock_worker_enabled:
        raise RuntimeError("LOCAL MOCK WORKER requires MOCK_WORKER_ENABLED=true")
    if settings.balance_db.name.lower() == "balance.db":
        raise RuntimeError("LOCAL MOCK WORKER refuses production balance.db")


def claim_one(settings: Settings) -> dict[str, Any] | None:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT job_code,user_id,prompt,charged_fen,mock_result
            FROM generation_tasks
            WHERE status='queued'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE generation_tasks
            SET status='processing', started_at=?, active_started_at=?
            WHERE job_code=? AND status='queued'
            """,
            (now, now, row["job_code"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mock_result_for(row: dict[str, Any]) -> str:
    explicit = str(row.get("mock_result") or "").strip().lower()
    if explicit in {"success", "failed", "timeout"}:
        return explicit
    prompt = str(row.get("prompt") or "").lower()
    if "[mock:failed]" in prompt:
        return "failed"
    if "[mock:timeout]" in prompt:
        return "timeout"
    return "success"


def _create_placeholder(settings: Settings, job_code: str) -> Path:
    output_dir = settings.mock_output_path.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_code}.png"
    image = Image.new("RGB", (768, 1024), (24, 34, 54))
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 36, 732, 988), outline=(126, 170, 255), width=6)
    draw.text((70, 120), "LOCAL MOCK", fill=(255, 255, 255))
    draw.text((70, 180), job_code, fill=(190, 220, 255))
    draw.text((70, 240), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(180, 255, 180))
    image.save(path)
    return path


def complete_success(settings: Settings, row: dict[str, Any]) -> None:
    now = int(time.time())
    job_code = str(row["job_code"])
    path = _create_placeholder(settings, job_code)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        if not current or current["status"] == "done":
            conn.rollback()
            return
        conn.execute(
            "INSERT OR IGNORE INTO generation_outputs(job_code,label,file_path,created_at) VALUES (?,?,?,?)",
            (job_code, "mock output", str(path), now),
        )
        conn.execute(
            "UPDATE generation_tasks SET status='done', effective_prompt=prompt, finished_at=? WHERE job_code=?",
            (now, job_code),
        )
        conn.commit()
        print(f"[LOCAL MOCK WORKER] completed job={job_code}", flush=True)
    finally:
        conn.close()


def complete_failed(settings: Settings, row: dict[str, Any]) -> None:
    now = int(time.time())
    job_code = str(row["job_code"])
    user_id = str(row["user_id"])
    charged = int(row.get("charged_fen") or 0)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT status FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
        if not current or current["status"] in {"done", "failed_refunded", "cancelled_refunded"}:
            conn.rollback()
            return
        existing = conn.execute(
            "SELECT id FROM balance_ledger WHERE order_code=? AND reason=? LIMIT 1",
            (job_code, MOCK_REFUND_REASON),
        ).fetchone()
        if charged and not existing:
            conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged, user_id))
            conn.execute(
                "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) VALUES (?,?,?,?,?,?)",
                (user_id, charged, MOCK_REFUND_REASON, job_code, "local_mock_worker", now),
            )
        conn.execute(
            "UPDATE generation_tasks SET status='failed_refunded', error='LOCAL MOCK failed', error_code='mock_failed', finished_at=? WHERE job_code=?",
            (now, job_code),
        )
        conn.commit()
        print(f"[LOCAL MOCK WORKER] failed_refunded job={job_code}", flush=True)
    finally:
        conn.close()


def leave_timeout(settings: Settings, row: dict[str, Any]) -> None:
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE generation_tasks SET error_code='mock_timeout' WHERE job_code=? AND status='processing'",
            (row["job_code"],),
        )
        conn.commit()
        print(f"[LOCAL MOCK WORKER] timeout_left_processing job={row['job_code']}", flush=True)
    finally:
        conn.close()


def process_once(settings: Settings) -> bool:
    row = claim_one(settings)
    if not row:
        return False
    result = _mock_result_for(row)
    if result == "timeout":
        leave_timeout(settings, row)
        return True
    time.sleep(max(0, int(settings.mock_generation_seconds)))
    if result == "failed":
        complete_failed(settings, row)
    else:
        complete_success(settings, row)
    return True


def run_forever(settings: Settings) -> None:
    validate_mock_environment(settings)
    settings.mock_output_path.mkdir(parents=True, exist_ok=True)
    print("[LOCAL MOCK WORKER] started", flush=True)
    while True:
        if not process_once(settings):
            time.sleep(max(1, int(settings.mock_worker_poll_seconds)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    validate_mock_environment(settings)
    if args.once:
        process_once(settings)
    else:
        run_forever(settings)


if __name__ == "__main__":
    main()
