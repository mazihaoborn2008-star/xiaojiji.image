from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.db import (
    allocate_negative_legacy_user_id,
    bind_account_legacy_user,
    connect,
    ensure_schema,
    get_account_legacy_user_id,
    grant_welcome_bonus_if_needed,
)


BACKUP_BASE = Path(r"E:\discord-BOT\balance_before_welcome_bonus_existing_users_20260717.db")


def next_backup_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def legacy_id_for_account(conn, account) -> str:
    existing = get_account_legacy_user_id(conn, account["id"])
    if existing:
        return existing
    if account["provider"] == "discord":
        legacy_id = str(account["provider_user_id"])
    else:
        legacy_id = allocate_negative_legacy_user_id(conn)
    bind_account_legacy_user(conn, account["id"], legacy_id)
    return legacy_id


def main() -> int:
    settings = get_settings()
    ensure_schema(settings)

    backup_path = next_backup_path(BACKUP_BASE)
    shutil.copy2(settings.balance_db, backup_path)

    total = 0
    granted = 0
    skipped = 0
    failed = 0

    conn = connect(settings)
    try:
        accounts = conn.execute(
            """
            SELECT id, provider, provider_user_id, welcome_credits_granted_at
            FROM accounts
            ORDER BY created_at, id
            """
        ).fetchall()
        total = len(accounts)
    finally:
        conn.close()

    for account in accounts:
        conn = connect(settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            legacy_id = legacy_id_for_account(conn, account)
            result = grant_welcome_bonus_if_needed(conn, account["id"], legacy_user_id=legacy_id)
            conn.commit()
            if result.get("granted"):
                granted += 1
            else:
                skipped += 1
        except Exception as exc:
            conn.rollback()
            failed += 1
            print(f"[WELCOME] account skipped reason=error account_id={account['id']} error={type(exc).__name__}", flush=True)
        finally:
            conn.close()

    print(f"[WELCOME] backup={backup_path}", flush=True)
    print(f"[WELCOME] total_users={total}", flush=True)
    print(f"[WELCOME] granted={granted}", flush=True)
    print(f"[WELCOME] skipped={skipped}", flush=True)
    print(f"[WELCOME] failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
