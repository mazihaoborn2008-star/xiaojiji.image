from __future__ import annotations

import shutil
import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"补丁 {label} 预期匹配 1 次，实际 {count} 次。请停止并人工检查。")
    return text.replace(old, new, 1)


def patch(source: Path, target: Path) -> None:
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        'BOT_VERSION = "2026-07-12-reroll-resolution-anima-fix"',
        'BOT_VERSION = "2026-07-16-web-mvp-bridge"',
        1,
    )
    text = replace_once(
        text,
        "worker_started = False\ncurrent_task = None",
        "worker_started = False\nqueue_watcher_started = False\ncurrent_task = None",
        "全局 watcher 状态",
    )

    schema_anchor = '''        if "auto_tagger" not in generation_columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN auto_tagger INTEGER NOT NULL DEFAULT 0")
'''
    schema_insert = schema_anchor + '''        if "source" not in generation_columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'discord'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_code TEXT NOT NULL,
                label TEXT,
                file_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(job_code, file_path)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_generation_outputs_job ON generation_outputs(job_code, id)")
'''
    text = replace_once(text, schema_anchor, schema_insert, "数据库 schema")

    function_anchor = '''def refund_processing_tasks_after_restart() -> list[dict]:
'''
    functions = '''def save_generation_outputs(job_code: str, outputs: list[tuple[str | None, str]]):
    """记录最终搬运后的图片路径，供网站按任务归属读取。"""
    init_balance_db()
    now = int(time.time())
    conn = sqlite3.connect(BALANCE_DB)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM generation_outputs WHERE job_code = ?", (job_code,))
        for label, file_path in outputs:
            conn.execute(
                "INSERT OR IGNORE INTO generation_outputs(job_code, label, file_path, created_at) VALUES (?, ?, ?, ?)",
                (job_code, label, os.path.abspath(file_path), now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


'''
    text = replace_once(text, function_anchor, functions + function_anchor, "输出记录函数")

    channel_anchor = '''async def get_task_output_channel(task: dict):
    channel = None
'''
    channel_new = '''class _SilentWebOutputChannel:
    """Web 任务使用的空输出通道：worker 继续复用，但不向 Discord 发消息。"""
    guild = None

    async def send(self, *args, **kwargs):
        file_obj = kwargs.get("file")
        if file_obj is not None and hasattr(file_obj, "close"):
            try:
                file_obj.close()
            except Exception:
                pass
        return None


async def get_task_output_channel(task: dict):
    if str(task.get("source") or "discord").lower() == "web":
        return _SilentWebOutputChannel()
    channel = None
'''
    text = replace_once(text, channel_anchor, channel_new, "Web 静默通道")

    recover_anchor = '''    if refunded or queued:
        print(f"启动恢复：已退款 processing 任务 {len(refunded)} 个，重新加入 queued 任务 {len(queued)} 个。")


'''
    watcher = recover_anchor + '''async def database_queue_watcher():
    """持续发现网站写入 SQLite 的 queued 任务。"""
    while True:
        try:
            queued = await asyncio.to_thread(get_queued_generation_tasks, MAX_QUEUE_SIZE)
            for task in queued:
                await enqueue_job_code(task["job_code"])
        except Exception as e:
            print(f"⚠️ 数据库队列 watcher 出错：{e}")
        await asyncio.sleep(2)


'''
    text = replace_once(text, recover_anchor, watcher, "数据库 watcher")

    text = replace_once(
        text,
        "async def on_ready():\n    global worker_started",
        "async def on_ready():\n    global worker_started, queue_watcher_started",
        "on_ready global",
    )
    ready_anchor = '''        worker_started = True
        await recover_generation_queue_on_startup()
'''
    ready_new = ready_anchor + '''    if not queue_watcher_started:
        bot.loop.create_task(database_queue_watcher())
        queue_watcher_started = True
'''
    text = replace_once(text, ready_anchor, ready_new, "启动 watcher")

    move_anchor = '''                moved_paths = []
                for idx, item in enumerate(valid_result_files, start=1):
                    label = item.get("label") or f"output{idx}"
                    safe_label = "".join(c for c in str(label) if c.isalnum() or c in ("_", "-", "一", "二", "次", "采", "样")) or f"output{idx}"
                    dst = os.path.join(BOT_OUTPUT_DIR, f"{job_code}_{safe_user}_{safe_label}_{int(time.time())}.png")
                    shutil.move(item["path"], dst)
                    moved_paths.append(dst)
                cleanup_generation_job_folders(job_code)
'''
    move_new = '''                moved_paths = []
                output_records = []
                for idx, item in enumerate(valid_result_files, start=1):
                    label = item.get("label") or f"output{idx}"
                    safe_label = "".join(c for c in str(label) if c.isalnum() or c in ("_", "-", "一", "二", "次", "采", "样")) or f"output{idx}"
                    dst = os.path.join(BOT_OUTPUT_DIR, f"{job_code}_{safe_user}_{safe_label}_{int(time.time())}.png")
                    shutil.move(item["path"], dst)
                    moved_paths.append(dst)
                    output_records.append((item.get("label"), dst))
                await asyncio.to_thread(save_generation_outputs, job_code, output_records)
                cleanup_generation_job_folders(job_code)
'''
    text = replace_once(text, move_anchor, move_new, "结果路径记录")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"已生成：{target}")


def main() -> None:
    if len(sys.argv) != 3:
        print('用法: python apply_bot_patch.py "原bot.py" "输出bot_web_mvp.py"')
        raise SystemExit(2)
    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == target:
        backup = source.with_suffix(source.suffix + ".before_web_mvp.bak")
        shutil.copy2(source, backup)
        print(f"已备份：{backup}")
    patch(source, target)


if __name__ == "__main__":
    main()
