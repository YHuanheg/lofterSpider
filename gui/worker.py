"""把任务放到后台线程跑，用队列跟界面通信。

tkinter 不是线程安全的：**所有**控件操作必须回到主线程。所以这里做两件事：

1. 任务在子线程里跑，日志/进度/提问都往 ``queue.Queue`` 里塞事件；
2. 界面用 ``after()`` 周期性取事件并更新控件。

原脚本的 6 处 ``input()`` 在 GUI 里必须变成模态弹窗，而不能阻塞主线程。
``QueueReporter.ask`` 会把问题丢给主线程、然后在子线程上带超时地等回答，
等待期间还会检查取消标志，所以「点取消」不会卡死。

任务结束时会连同 :class:`~lofter.tasks.base.TaskResult` 一起投递，
界面拿它渲染统计卡片——这正是 wallpaper 里 ``run_*() -> Stats`` 的用法。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Optional

from lofter.errors import LofterError, TaskCancelled
from lofter.reporter import Reporter
from lofter.tasks import TaskResult, get_task, run_task

__all__ = ["QueueReporter", "BackgroundRunner", "Event"]

Event = tuple  # (kind, ...) 见 QueueReporter 里各方法的说明


class QueueReporter(Reporter):
    """把反馈事件投递到队列，由主线程消费。"""

    interactive = True

    def __init__(self, events: "queue.Queue[Event]") -> None:
        super().__init__()
        self.events = events

    # -------------------------------------------------------------- 事件投递
    def _emit(self, level: int, msg: str) -> None:
        self.events.put(("log", level, msg))

    def stage(self, name: str) -> None:
        super().stage(name)
        self.events.put(("stage", name))

    def progress(self, current: int, total: int, desc: str = "") -> None:
        self.events.put(("progress", current, total, desc))

    # ---------------------------------------------------------------- 交互
    def _round_trip(self, kind: str, question: str, default: Any) -> Any:
        reply: "queue.Queue[Any]" = queue.Queue(1)
        self.events.put((kind, question, default, reply))
        while True:
            try:
                return reply.get(timeout=0.2)
            except queue.Empty:
                # 超时轮询而不是无限等待：这样「取消」才有机会生效
                self.check_cancel()

    def ask(self, question: str, default: str = "") -> str:
        value = self._round_trip("ask", question, default)
        if value is None:
            raise TaskCancelled("用户在提问时取消了任务")
        return str(value)

    def confirm(self, question: str, default: bool = False) -> bool:
        value = self._round_trip("confirm", question, default)
        if value is None:
            raise TaskCancelled("用户在确认时取消了任务")
        return bool(value)


class BackgroundRunner:
    """一次只跑一个任务的后台执行器。"""

    def __init__(self) -> None:
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.reporter: Optional[QueueReporter] = None
        self.thread: Optional[threading.Thread] = None
        self._started_at = 0.0
        self.tid = ""
        self.base_dir = ""
        self.result: Optional[TaskResult] = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def elapsed(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    def start(self, tid: str, cfg, account) -> None:
        if self.busy:
            raise RuntimeError("已经有一个任务在跑了")

        self.tid = tid
        self.reporter = QueueReporter(self.events)
        self._started_at = time.time()
        self.result = None
        self.base_dir = get_task(tid).base_dir_of(cfg)

        def body() -> None:
            try:
                # 统一走 run_task：计时、结果归一化、账号校验只有一份实现
                result = run_task(tid, cfg, account=account, reporter=self.reporter,
                                  base_dir=self.base_dir)
                self.result = result
                self.events.put(("done", True, result.message or "任务完成", result))
            except TaskCancelled:
                self.events.put(("done", False, "已取消", None))
            except LofterError as exc:
                self.events.put(("done", False, str(exc), None))
            except Exception as exc:  # 兜底，避免线程静默死掉
                import traceback

                self.events.put(("log", 40, traceback.format_exc()))
                self.events.put(
                    ("done", False, "未预期的错误：{}: {}".format(type(exc).__name__, exc), None))

        self.thread = threading.Thread(target=body, name="lofter-task", daemon=True)
        self.thread.start()

    def cancel(self) -> None:
        if self.reporter is not None:
            self.reporter.request_cancel()
