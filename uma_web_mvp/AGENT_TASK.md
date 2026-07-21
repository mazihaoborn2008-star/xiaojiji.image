# 给 Codex / Hermes Agent 的执行任务

把下面整段发给你的代码 Agent：

```text
你正在修改 <PROJECT_ROOT> 项目。目标是让 Discord Bot 与 FastAPI 网站共用 SQLite 任务队列，且只能有 Discord Bot 的 image_worker 调用 ComfyUI。

硬性要求：
1. 不要删除现有充值、退款、重启恢复、图生图、ControlNet、Anima 双采样功能。
2. 不要让 Web 进程 import bot.py，也不要让 Web 直接请求 ComfyUI。
3. 先备份原 bot.py。
4. 运行 uma_web_mvp/apply_bot_patch.py 生成 bot_web_mvp.py，不要直接覆盖原文件。
5. 检查补丁后必须确认：
   - generation_tasks 新增 source，旧任务默认 discord；
   - generation_outputs 存储任务结果路径；
   - database_queue_watcher 每 2 秒把数据库 queued 任务加入内存队列；
   - mark_generation_processing 仍是 queued -> processing 的原子保护；
   - source=web 使用静默输出通道，不向 Discord 发进度和图片；
   - 成功移动图片后写 generation_outputs；
   - 网站任务失败和取消仍按原账本逻辑退款。
6. 执行：python -m py_compile bot_web_mvp.py
7. 执行：python -m compileall uma_web_mvp/app
8. 不要启动真实 Discord Bot 或 ComfyUI；只做静态检查。
9. 最后给出改动文件列表、关键 diff、发现的风险和本机测试命令。
```

第二阶段再让 Agent 把 Bot 和 Web 共用的数据库函数抽到 `shared/`。第一阶段不要大重构，否则容易把当前可用 Bot 改坏。
