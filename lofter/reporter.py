"""运行期反馈通道：日志、进度、交互确认、取消。

任务代码只跟这个接口打交道，因此同一份任务代码既能跑在终端里（``CliReporter``），
也能跑在 tkinter 后台线程里（``gui.worker.QueueReporter``），还能跑在测试里
（``NullReporter``）。

原脚本里散落着 6 处 ``input()``：
    l13 阶段3→阶段4 的 ok 闸门 / 结尾 yes|no 重置
    l4  启动前的 ok 闸门 / 作者名解析失败时手动输入
    l9  模板0 风险确认
    l9  章节合并日期重复时手动排序
这些全部改走 :meth:`Reporter.ask` / :meth:`Reporter.confirm`。
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Optional

from .errors import NeedUserInput, TaskCancelled

__all__ = ["Reporter", "CliReporter", "NullReporter", "LEVEL_NAMES"]


LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR"}


class Reporter:
    """反馈接口。子类至少要重写 :meth:`_emit`。"""

    #: 默认日志等级，10=debug 20=info 30=warn 40=error
    min_level: int = 10
    #: 是否能向用户提问。原脚本有 6 处 ``input()``，无人值守运行时不能卡在那里，
    #: 因此任务代码遇到这些点会先看 :attr:`interactive`，不可交互时走安全默认值。
    interactive: bool = False

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._stage = ""

    # ------------------------------------------------------------------ 日志
    def _emit(self, level: int, msg: str) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def log(self, msg: str = "", level: int = 20) -> None:
        if level >= self.min_level:
            self._emit(level, msg)

    def debug(self, msg: str) -> None:
        self.log(msg, 10)

    def warn(self, msg: str) -> None:
        self.log(msg, 30)

    def error(self, msg: str) -> None:
        self.log(msg, 40)

    def stage(self, name: str) -> None:
        """宣告进入一个新阶段，供 GUI 显示当前状态。"""
        self._stage = name
        self.log("=" * 8 + " " + name + " " + "=" * 8, 20)

    # ---------------------------------------------------------------- 进度条
    def progress(self, current: int, total: int, desc: str = "") -> None:
        """上报进度。``total`` 为 0 表示不确定进度。"""

    # ------------------------------------------------------------------ 交互
    def ask(self, question: str, default: str = "") -> str:
        """开放式提问，返回用户输入。"""
        raise NeedUserInput(question)

    def confirm(self, question: str, default: bool = False) -> bool:
        """是/否确认。"""
        raise NeedUserInput(question)

    # ------------------------------------------------------------------ 取消
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def check_cancel(self) -> None:
        """在长循环里调用；被取消时抛 :class:`TaskCancelled`。"""
        if self.cancelled:
            raise TaskCancelled("任务已被用户取消")


class CliReporter(Reporter):
    """终端实现：print 到 stdout，input 读 stdin，Ctrl+C 视作取消。"""

    def __init__(self, verbose: bool = False, stream=None) -> None:
        super().__init__()
        self.min_level = 10 if verbose else 20
        self.interactive = True
        self._stream = stream or sys.stdout

    def _emit(self, level: int, msg: str) -> None:
        prefix = "" if level == 20 else "[{}] ".format(LEVEL_NAMES.get(level, level))
        line = prefix + str(msg).replace("\n", "\n" + " " * len(prefix))
        try:
            print(line, file=self._stream, flush=True)
        except UnicodeEncodeError:
            # Windows 控制台默认 GBK，遇到 emoji 会炸（原 l9 就踩过这个坑）
            enc = getattr(self._stream, "encoding", None) or "utf-8"
            print(line.encode(enc, errors="replace").decode(enc, errors="replace"),
                  file=self._stream, flush=True)

    def progress(self, current: int, total: int, desc: str = "") -> None:
        if total:
            print("进度 {}/{} {}".format(current, total, desc), file=self._stream, flush=True)

    def ask(self, question: str, default: str = "") -> str:
        try:
            answer = input(question)
        except (EOFError, KeyboardInterrupt) as exc:
            raise TaskCancelled("输入被中断") from exc
        return answer.strip() or default

    def confirm(self, question: str, default: bool = False) -> bool:
        hint = " [Y/n] " if default else " [y/N] "
        answer = self.ask(question + hint).strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes", "ok", "是")


class NullReporter(Reporter):
    """测试用：丢弃一切输出，need_input 时给出预设回答。"""

    def __init__(self, answers: Optional[list[Any]] = None) -> None:
        super().__init__()
        self.lines: list[tuple[int, str]] = []
        self.progress_events: list[tuple[int, int, str]] = []
        self.stages: list[str] = []
        self._answers = list(answers or [])
        self._hooks: dict[str, Callable[..., Any]] = {}

    def _emit(self, level: int, msg: str) -> None:
        self.lines.append((level, msg))

    def stage(self, name: str) -> None:
        self.stages.append(name)
        super().stage(name)

    def progress(self, current: int, total: int, desc: str = "") -> None:
        self.progress_events.append((current, total, desc))

    def _next_answer(self, question: str, fallback: Any) -> Any:
        for key, hook in self._hooks.items():
            if key in question:
                return hook(question)
        if self._answers:
            return self._answers.pop(0)
        return fallback

    def ask(self, question: str, default: str = "") -> str:
        value = self._next_answer(question, default)
        return str(value)

    def confirm(self, question: str, default: bool = False) -> bool:
        value = self._next_answer(question, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("y", "yes", "ok", "是", "true", "1")

    def on(self, keyword: str, hook: Callable[..., Any]) -> None:
        """按问题关键字挂钩子，避免依赖提问顺序。"""
        self._hooks[keyword] = hook

    @property
    def text(self) -> str:
        return "\n".join(m for _, m in self.lines)
