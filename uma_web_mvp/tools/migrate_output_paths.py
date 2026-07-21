#!/usr/bin/env python3
"""
迁移 generation_outputs.file_path 从绝对路径 → 相对路径（纯文件名）

用法:
    python migrate_output_paths.py            # dry-run，不修改数据库
    python migrate_output_paths.py --apply    # 实际迁移（自动备份）

要求:
    - BOT_OUTPUT_DIR = E:\discord-BOT\output
    - 所有文件应位于 BOT_OUTPUT_DIR 内
    - 可在 Windows 或 WSL 下运行，自动适配路径
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path


def _to_native_path(win_path: str) -> Path:
    """将 Windows 路径转为当前 OS 可用的路径。WSL 下 E:\foo → /mnt/e/foo。"""
    p = Path(win_path)
    if p.is_absolute():
        return p
    # Windows drive letter on non-Windows → convert
    m = re.match(r'^([A-Za-z]):[\\/](.*)', win_path)
    if m and sys.platform != 'win32':
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        return Path(f'/mnt/{drive}/{rest}')
    return p


BOT_OUTPUT_DIR = _to_native_path(r"E:\discord-BOT\output")
BALANCE_DB = _to_native_path(r"E:\discord-BOT\balance.db")
BACKUP_DB = _to_native_path(r"E:\discord-BOT\balance_before_output_path_migration.db")


def resolve_abs(stored_path: str) -> Path | None:
    """将存储路径解析为绝对路径，无法解析返回 None。
    兼容 Windows 绝对路径 (E:\\)、WSL 路径 (/mnt/e/)、相对路径。"""
    if not stored_path:
        return None
    norm = stored_path

    # 检测并转换 Windows 盘符路径 (WSL 下转为 /mnt/e/...)
    m = re.match(r'^([A-Za-z]):[\\/](.*)', norm)
    if m and sys.platform != 'win32':
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        candidate = Path(f'/mnt/{drive}/{rest}')
    elif os.path.isabs(norm):
        candidate = Path(norm).resolve()
    else:
        candidate = (BOT_OUTPUT_DIR / norm).resolve()

    # 必须在 BOT_OUTPUT_DIR 内
    bot_abs = BOT_OUTPUT_DIR.resolve()
    if bot_abs not in candidate.parents and candidate != bot_abs:
        return None
    # 文件必须存在
    if not candidate.is_file():
        return None
    return candidate


def migrate(dry_run: bool = True):
    """迁移主函数。dry_run=True 时不修改数据库。"""
    if not BALANCE_DB.exists():
        print(f"[ERROR] 数据库不存在: {BALANCE_DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(BALANCE_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. 读取所有记录
    cur.execute("SELECT id, job_code, file_path, label, created_at FROM generation_outputs ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    print(f"总记录数: {len(rows)}")
    print()

    # 2. 分类统计
    to_migrate = []   # 可迁移记录
    skip = []         # 跳过记录
    already_rel = []  # 已经是相对路径

    for r in rows:
        fp = r["file_path"] or ""
        abs_path = resolve_abs(fp)
        if abs_path is None:
            reason = "无法解析或文件不存在"
            skip.append((r, reason))
        else:
            rel = abs_path.name  # 纯文件名
            if fp == rel:
                already_rel.append(r)
            else:
                to_migrate.append((r, abs_path, rel))

    print(f"可迁移 (绝对路径 → 相对路径): {len(to_migrate)}")
    print(f"跳过 (无法解析/文件不存在):    {len(skip)}")
    print(f"已经是相对路径:               {len(already_rel)}")
    print()

    if to_migrate:
        print("=" * 60)
        print("可迁移记录详情:")
        for r, abs_path, rel in to_migrate:
            print(f"  id={r['id']:>4}  job={r['job_code']}")
            print(f"          旧: {r['file_path']}")
            print(f"          新: {rel}")
            print()

    if skip:
        print("=" * 60)
        print("跳过记录详情:")
        for r, reason in skip:
            print(f"  id={r['id']:>4}  job={r['job_code']}  原因: {reason}")
            print(f"          路径: {r['file_path']}")
            print()

    if dry_run:
        print("=" * 60)
        print("[DRY-RUN] 未修改数据库。")
        print("如需实际迁移，请运行: python migrate_output_paths.py --apply")
        return

    # 3. --apply: 备份 + 迁移
    print("=" * 60)
    print(f"备份数据库 → {BACKUP_DB} ...")
    shutil.copy2(str(BALANCE_DB), str(BACKUP_DB))
    print("备份完成。")
    print()

    if not to_migrate:
        print("没有需要迁移的记录。")
        return

    conn = sqlite3.connect(str(BALANCE_DB))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")

        updated = 0
        for r, abs_path, rel in to_migrate:
            conn.execute(
                "UPDATE generation_outputs SET file_path = ? WHERE id = ?",
                (rel, r["id"]),
            )
            updated += 1

        conn.commit()
        print(f"[OK] 成功迁移 {updated} 条记录。（事务已提交）")

        # 验证
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, file_path FROM generation_outputs WHERE id IN ({})".format(
            ",".join(str(r[0]["id"]) for r in to_migrate)
        ))
        for row in cur.fetchall():
            path = row["file_path"]
            if os.path.isabs(path):
                print(f"  ⚠ 验证失败: id={row['id']} 仍是绝对路径: {path}")
            else:
                print(f"  ✓ id={row['id']}: {path}")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] 迁移失败，已回滚: {e}")
        sys.exit(1)
    finally:
        conn.close()

    print()
    print("=" * 60)
    print("迁移完成！")
    print(f"  备份: {BACKUP_DB}")
    print(f"  迁移: {updated} 条")
    print(f"  跳过: {len(skip)} 条")

    if skip:
        print()
        print("提示：跳过的记录需要手动处理。可能是文件不存在或路径在 BOT_OUTPUT_DIR 之外。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="迁移 generation_outputs.file_path 到相对路径")
    parser.add_argument("--apply", action="store_true", help="实际执行迁移（默认 dry-run）")
    args = parser.parse_args()
    migrate(dry_run=not args.apply)
