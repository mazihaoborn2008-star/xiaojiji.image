# Bot 补丁说明

补丁只增加四件事：

1. `generation_tasks.source`：区分 Discord 与 Web 来源。
2. `generation_outputs`：记录网站可读取的最终图片路径。
3. `database_queue_watcher()`：网站写入 SQLite 后，Bot 不重启也能发现任务。
4. `_SilentWebOutputChannel`：Web 任务不往 Discord 频道或私信发送内容。

不让网站直接调用 `generate_image()`，因为你当前的扣费、状态、失败退款和重启恢复都围绕 SQLite 任务表；两个进程同时直接调用 ComfyUI 会产生双 worker、顺序错乱和资源竞争。并且 `bot.py` 底部会执行 `bot.run(TOKEN)`，不能被网站直接 import。
