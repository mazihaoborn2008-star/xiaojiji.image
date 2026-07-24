from __future__ import annotations

import secrets
import sqlite3
import string
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

MIN_TOPUP_FEN = 100
MAX_TOPUP_FEN = 50000
MAX_PENDING_TOPUPS_PER_USER = 3
ORDER_EXPIRE_SECONDS = 24 * 60 * 60
PAYMENT_METHOD_WECHAT = "wechat_qr"
PAYMENT_METHOD_ASB = "asb_bank_transfer"
ASB_CREDIT_PACKAGES: dict[int, str] = {
    100: "1.00",
    200: "2.00",
    500: "5.00",
    1000: "10.00",
}
WECHAT_CREATED_EXPIRE_SECONDS = 24 * 60 * 60
WECHAT_PAID_REVIEW_SECONDS = 48 * 60 * 60
ASB_CREATED_EXPIRE_SECONDS = 7 * 24 * 60 * 60
ASB_PAID_REVIEW_SECONDS = 7 * 24 * 60 * 60


def connect_recharge_db(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def ensure_recharge_schema(db_path: Path | str) -> None:
    conn = connect_recharge_db(db_path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance_fen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS recharge_requests (
            code TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            amount_fen INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            reviewed_at INTEGER,
            reviewer_id TEXT,
            reviewer_name TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS balance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount_fen INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_code TEXT,
            operator_id TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recharge_user_status ON recharge_requests(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_recharge_status_time ON recharge_requests(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_ledger_user_time ON balance_ledger(user_id, created_at);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recharge_requests)").fetchall()}
        additions = {
            "source": "TEXT NOT NULL DEFAULT 'discord'",
            "owner_notify_status": "TEXT NOT NULL DEFAULT 'none'",
            "owner_notified_at": "INTEGER",
            "owner_notify_attempts": "INTEGER NOT NULL DEFAULT 0",
            "owner_notify_error": "TEXT",
            "payment_method": f"TEXT NOT NULL DEFAULT '{PAYMENT_METHOD_WECHAT}'",
            "payment_reference": "TEXT",
            "currency": "TEXT",
            "credits": "INTEGER",
            "expires_at": "INTEGER",
            "paid_expires_at": "INTEGER",
            "cancelled_at": "INTEGER",
            "cancel_reason": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE recharge_requests ADD COLUMN {name} {ddl}")
        conn.execute(
            """
            UPDATE recharge_requests
            SET payment_method=COALESCE(NULLIF(payment_method, ''), ?),
                currency=COALESCE(NULLIF(currency, ''), 'RMB'),
                credits=COALESCE(credits, amount_fen),
                payment_reference=COALESCE(NULLIF(payment_reference, ''), code),
                expires_at=COALESCE(expires_at, created_at + ?)
            """,
            (PAYMENT_METHOD_WECHAT, WECHAT_CREATED_EXPIRE_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()


def fen_to_rmb_text(fen: int) -> str:
    return f"{int(fen) / 100:.2f} RMB"


def parse_rmb_to_fen(amount_rmb: str | int | float | Decimal) -> int:
    try:
        value = Decimal(str(amount_rmb)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("金额格式不正确。")
    return int(value * 100)


def validate_topup_amount_fen(amount_fen: int) -> None:
    amount_fen = int(amount_fen)
    if amount_fen < MIN_TOPUP_FEN:
        raise ValueError(f"最低充值金额是 {fen_to_rmb_text(MIN_TOPUP_FEN)}。")
    if amount_fen > MAX_TOPUP_FEN:
        raise ValueError(f"单笔最高充值金额是 {fen_to_rmb_text(MAX_TOPUP_FEN)}。")


def normalize_payment_method(value: str | None) -> str:
    method = (value or PAYMENT_METHOD_WECHAT).strip().lower()
    if method in {"wechat", "wechat_qr"}:
        return PAYMENT_METHOD_WECHAT
    if method in {"asb", "asb_bank_transfer"}:
        return PAYMENT_METHOD_ASB
    raise ValueError("支付方式无效。")


def validate_asb_credits(credits: int | str | None) -> int:
    try:
        value = int(str(credits or "").strip())
    except (TypeError, ValueError):
        raise ValueError("请选择有效的 ASB credits 套餐。")
    if value not in ASB_CREDIT_PACKAGES:
        raise ValueError("请选择有效的 ASB credits 套餐。")
    return value


def asb_nzd_amount_for_credits(credits: int) -> str:
    return ASB_CREDIT_PACKAGES[int(credits)]


def topup_amount_text(order: dict[str, Any]) -> str:
    method = order_payment_method(order)
    if method == PAYMENT_METHOD_ASB:
        credits = int(order.get("credits") or order.get("amount_fen") or 0)
        return f"NZD ${asb_nzd_amount_for_credits(credits)}"
    return fen_to_rmb_text(int(order.get("amount_fen") or 0))


def topup_credit_text(order: dict[str, Any]) -> str:
    credits = int(order.get("credits") or order.get("amount_fen") or 0)
    return f"{credits} credits"


def order_payment_method(order: dict[str, Any] | sqlite3.Row) -> str:
    try:
        method = order["payment_method"]
    except Exception:
        method = None
    try:
        return normalize_payment_method(method)
    except ValueError:
        return PAYMENT_METHOD_WECHAT


def payment_method_label(order: dict[str, Any]) -> str:
    return "ASB Bank Transfer" if order_payment_method(order) == PAYMENT_METHOD_ASB else "微信扫码"


def created_expire_seconds_for_method(payment_method: str) -> int:
    return ASB_CREATED_EXPIRE_SECONDS if payment_method == PAYMENT_METHOD_ASB else WECHAT_CREATED_EXPIRE_SECONDS


def paid_review_seconds_for_method(payment_method: str) -> int:
    return ASB_PAID_REVIEW_SECONDS if payment_method == PAYMENT_METHOD_ASB else WECHAT_PAID_REVIEW_SECONDS


def created_expire_seconds(
    payment_method: str,
    *,
    wechat_hours: int | None = None,
    asb_days: int | None = None,
) -> int:
    if payment_method == PAYMENT_METHOD_ASB:
        days = max(1, int(asb_days or 7))
        return days * 24 * 60 * 60
    hours = max(1, int(wechat_hours or 24))
    return hours * 60 * 60


def paid_review_seconds(
    payment_method: str,
    *,
    asb_days: int | None = None,
) -> int:
    if payment_method == PAYMENT_METHOD_ASB:
        days = max(1, int(asb_days or 7))
        return days * 24 * 60 * 60
    return WECHAT_PAID_REVIEW_SECONDS


def generate_recharge_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "PAY-" + "".join(secrets.choice(alphabet) for _ in range(14))


def create_recharge_request_for_identity(
    db_path: Path | str,
    *,
    user_id: str,
    username: str,
    amount_fen: int | None = None,
    source: str = "web",
    payment_method: str = PAYMENT_METHOD_WECHAT,
    credits: int | None = None,
    asb_enabled: bool = False,
    wechat_expires_hours: int | None = None,
    asb_expires_days: int | None = None,
) -> str:
    ensure_recharge_schema(db_path)
    payment_method = normalize_payment_method(payment_method)
    currency = "RMB"
    if payment_method == PAYMENT_METHOD_ASB:
        if not asb_enabled:
            raise ValueError("ASB Bank Transfer is currently unavailable.")
        credit_value = validate_asb_credits(credits)
        amount_fen = credit_value
        currency = "NZD"
    else:
        if amount_fen is None:
            raise ValueError("金额格式不正确。")
        amount_fen = int(amount_fen)
        validate_topup_amount_fen(amount_fen)
        credit_value = amount_fen
    now = int(time.time())
    expires_at = now + created_expire_seconds(
        payment_method,
        wechat_hours=wechat_expires_hours,
        asb_days=asb_expires_days,
    )
    conn = connect_recharge_db(db_path)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM recharge_requests WHERE user_id=? AND status IN ('created', 'paid')",
            (str(user_id),),
        ).fetchone()[0]
        if int(pending) >= MAX_PENDING_TOPUPS_PER_USER:
            raise RuntimeError(f"你还有 {pending} 笔充值申请未处理，请先完成或联系群主处理后再发起。")
        for _ in range(10):
            code = generate_recharge_code()
            try:
                conn.execute(
                    """
                    INSERT INTO recharge_requests
                    (
                        code, user_id, username, amount_fen, status, created_at,
                        source, owner_notify_status, payment_method, payment_reference,
                        currency, credits, expires_at
                    )
                    VALUES (?, ?, ?, ?, 'created', ?, ?, 'none', ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        str(user_id),
                        str(username)[:120],
                        int(amount_fen),
                        now,
                        str(source)[:32],
                        payment_method,
                        code,
                        currency,
                        int(credit_value),
                        int(expires_at),
                    ),
                )
                conn.commit()
                return code
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("生成充值单号失败，请重试。")
    finally:
        conn.close()


def get_recharge_request(db_path: Path | str, code: str) -> dict[str, Any] | None:
    ensure_recharge_schema(db_path)
    conn = connect_recharge_db(db_path)
    try:
        row = conn.execute("SELECT * FROM recharge_requests WHERE code=?", (code.strip().upper(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_recharge_requests_for_user(db_path: Path | str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    ensure_recharge_schema(db_path)
    conn = connect_recharge_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM recharge_requests WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (str(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_pending_topup_for_user(
    db_path: Path | str,
    user_id: str,
    payment_method: str | None = None,
) -> dict[str, Any] | None:
    """Find the most recent active (created/paid) topup order for a user.

    Returns the most recent order matching the payment method (if specified),
    otherwise returns any pending order. Only returns orders in 'created' or 'paid' status.
    """
    ensure_recharge_schema(db_path)
    conn = connect_recharge_db(db_path)
    try:
        if payment_method:
            row = conn.execute(
                """SELECT * FROM recharge_requests
                   WHERE user_id=? AND status IN ('created', 'paid')
                     AND payment_method=?
                   ORDER BY created_at DESC LIMIT 1""",
                (str(user_id), payment_method),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM recharge_requests
                   WHERE user_id=? AND status IN ('created', 'paid')
                   ORDER BY created_at DESC LIMIT 1""",
                (str(user_id),),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cancel_recharge_request(
    db_path: Path | str,
    code: str,
    user_id: str,
) -> tuple[bool, str]:
    """Cancel a recharge request that is in 'created' status.

    Returns (ok, message). Only 'created' orders can be cancelled.
    Does NOT delete the record — sets status to 'cancelled' and records cancel time.
    """
    ensure_recharge_schema(db_path)
    now = int(time.time())
    code = code.strip().upper()
    conn = connect_recharge_db(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, status FROM recharge_requests WHERE code=?",
            (code,),
        ).fetchone()
        if not row:
            return False, "找不到这笔充值申请。"
        if str(row["user_id"]) != str(user_id):
            return False, "这不是你的充值申请，不能取消。"
        if row["status"] != "created":
            return False, "该订单状态不允许取消，只有待付款的订单可以取消。"
        conn.execute(
            """UPDATE recharge_requests
               SET status='cancelled', cancelled_at=?, cancel_reason='user_cancelled'
               WHERE code=? AND status='created'""",
            (now, code),
        )
        conn.commit()
        return True, "订单已取消。"
    finally:
        conn.close()


def mark_recharge_paid_for_user(
    db_path: Path | str,
    code: str,
    user_id: str,
    *,
    wechat_expires_hours: int | None = None,
    asb_expires_days: int | None = None,
    paid_review_days: int | None = None,
) -> tuple[bool, str]:
    ensure_recharge_schema(db_path)
    now = int(time.time())
    code = code.strip().upper()
    conn = connect_recharge_db(db_path)
    try:
        row = conn.execute(
            "SELECT user_id, status, created_at, payment_method, expires_at FROM recharge_requests WHERE code=?",
            (code,),
        ).fetchone()
        if not row:
            return False, "找不到这笔充值申请。"
        if str(row["user_id"]) != str(user_id):
            return False, "这不是你的充值申请，不能提交。"
        if row["status"] != "created":
            return False, f"这笔申请当前状态是 {row['status']}，不能重复提交。"
        method = order_payment_method(row)
        expires_at = int(row["expires_at"] or (int(row["created_at"]) + created_expire_seconds(
            method,
            wechat_hours=wechat_expires_hours,
            asb_days=asb_expires_days,
        )))
        if now > expires_at:
            conn.execute("UPDATE recharge_requests SET status='expired' WHERE code=? AND status='created'", (code,))
            conn.commit()
            if method == PAYMENT_METHOD_ASB:
                return False, "This top-up order has expired. Please create a new order."
            return False, "这笔充值申请已超过 24 小时，请重新发起。"
        paid_expires_at = now + paid_review_seconds(method, asb_days=paid_review_days)
        cur = conn.execute(
            """
            UPDATE recharge_requests
            SET status='paid', paid_at=?, paid_expires_at=?, owner_notify_status='pending', owner_notify_error=NULL
            WHERE code=? AND status='created'
            """,
            (now, paid_expires_at, code),
        )
        conn.commit()
        if method == PAYMENT_METHOD_ASB:
            return cur.rowcount == 1, "Submitted for admin review."
        return cur.rowcount == 1, "已提交给群主审核。"
    finally:
        conn.close()


def claim_owner_notifications(db_path: Path | str, limit: int = 5) -> list[dict[str, Any]]:
    ensure_recharge_schema(db_path)
    now = int(time.time())
    conn = connect_recharge_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM recharge_requests
            WHERE status='paid' AND owner_notify_status='pending'
            ORDER BY paid_at ASC, created_at ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        codes = [r["code"] for r in rows]
        for code in codes:
            conn.execute(
                """
                UPDATE recharge_requests
                SET owner_notify_status='sending', owner_notify_attempts=owner_notify_attempts+1, owner_notify_error=NULL
                WHERE code=? AND owner_notify_status='pending'
                """,
                (code,),
            )
        conn.commit()
        return [dict(r) for r in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_owner_notification_sent(db_path: Path | str, code: str) -> None:
    ensure_recharge_schema(db_path)
    conn = connect_recharge_db(db_path)
    try:
        conn.execute(
            "UPDATE recharge_requests SET owner_notify_status='sent', owner_notified_at=?, owner_notify_error=NULL WHERE code=?",
            (int(time.time()), code.strip().upper()),
        )
        conn.commit()
    finally:
        conn.close()


def mark_owner_notification_failed(db_path: Path | str, code: str, error: str) -> None:
    ensure_recharge_schema(db_path)
    conn = connect_recharge_db(db_path)
    try:
        conn.execute(
            "UPDATE recharge_requests SET owner_notify_status='pending', owner_notify_error=? WHERE code=?",
            (str(error)[:200], code.strip().upper()),
        )
        conn.commit()
    finally:
        conn.close()
