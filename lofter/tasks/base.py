"""任务基类、运行上下文与结果摘要。

``BaseTask`` 只声明「我是谁、我要什么配置、我干什么」，具体怎么把日志显示出来、
怎么在界面上点「开始」，都不属于任务本身。这样同一份任务代码可以：

* 被 CLI 直接调用（``--task collection``）；
* 被 GUI 丢进后台线程跑（``gui.worker.QueueReporter``）；
* 被测试用 ``NullReporter`` 离线跑（不发真实请求）。

任务通过 ``TaskResult`` 把「干了多少活」结构化地交回来——这个做法借鉴了另一个
项目（wallpaper）里 ``run_*() -> XxxStats`` 的设计：界面拿它渲染统计卡片、
命令行拿它打印汇总、测试拿它做断言，比让上层去正则抠日志文本可靠得多。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field as dc_field
from typing import Optional

from ..config import Account, BaseConfig, resolve_base_dir
from ..net import get_headers, login_session, make_session
from ..reporter import Reporter
from ..utils import safe_makedirs

__all__ = ["TaskResult", "TaskContext", "BaseTask"]


@dataclass
class TaskResult:
    """一次任务的产出摘要。

    :param stats: 自由形式的计数，键会直接显示在界面的统计卡片上。
        建议用中文短标签，例如 ``{"已保存章节": 42, "跳过": 3, "失败": 0}``。
    """

    task: str = ""
    ok: bool = True
    message: str = ""
    output_dir: str = ""
    elapsed: float = 0.0
    stats: dict = dc_field(default_factory=dict)

    def summary(self) -> str:
        """一行文字摘要，命令行与日志用。"""
        head = "成功" if self.ok else "失败"
        parts = ["{}：{}".format(self.task, head)]
        if self.message:
            parts.append(self.message)
        for key, value in self.stats.items():
            parts.append("{} {}".format(key, value))
        if self.elapsed:
            parts.append("耗时 {:.1f}s".format(self.elapsed))
        return "｜".join(parts)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "ok": self.ok,
            "message": self.message,
            "output_dir": self.output_dir,
            "elapsed": round(self.elapsed, 2),
            "stats": dict(self.stats),
        }


class TaskContext:
    """一次任务运行的全部外部依赖。

    除了账号 / 日志 / 输出目录 / Session，还带一个 :attr:`stats` 累加器：
    任务里随时 ``ctx.count("已保存章节")``，结束时 ``ctx.make_result()`` 自动带上。
    """

    def __init__(self, account: Account, reporter: Reporter, base_dir: str,
                 interactive: Optional[bool] = None) -> None:
        self.account = account
        self.reporter = reporter
        self.base_dir = base_dir
        self.stats: dict = {}
        self.started_at = time.time()
        if interactive is not None:
            reporter.interactive = interactive
        safe_makedirs(base_dir)

    # ------------------------------------------------------------ 日志代理
    def log(self, msg: str = "", level: int = 20) -> None:
        self.reporter.log(msg, level)

    def debug(self, msg: str) -> None:
        self.reporter.debug(msg)

    def warn(self, msg: str) -> None:
        self.reporter.warn(msg)

    def error(self, msg: str) -> None:
        self.reporter.error(msg)

    def stage(self, name: str) -> None:
        self.reporter.stage(name)

    def progress(self, current: int, total: int, desc: str = "") -> None:
        self.reporter.progress(current, total, desc)

    # -------------------------------------------------------------- 统计
    def count(self, key: str, delta: int = 1) -> None:
        """累加一个计数。同名键相加，适合在循环里随手调用。"""
        self.stats[key] = self.stats.get(key, 0) + delta

    def set_stat(self, key: str, value) -> None:
        self.stats[key] = value

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def make_result(self, *, ok: bool = True, message: str = "",
                    output_dir: str = "") -> TaskResult:
        return TaskResult(
            task="",
            ok=ok,
            message=message,
            output_dir=output_dir or self.base_dir,
            elapsed=self.elapsed,
            stats=dict(self.stats),
        )

    # ------------------------------------------------------------ 交互代理
    @property
    def interactive(self) -> bool:
        return bool(getattr(self.reporter, "interactive", False))

    def ask(self, question: str, default: str = "") -> str:
        return self.reporter.ask(question, default)

    def confirm(self, question: str, default: bool = False) -> bool:
        return self.reporter.confirm(question, default)

    def confirm_if_interactive(self, question: str, default: bool = False) -> bool:
        """可交互时提问；无人值守时直接返回 ``default``，不阻塞。"""
        if not self.interactive:
            self.debug("（无人值守模式，自动沿用默认答案：{}）{}".format(default, question))
            return default
        return self.reporter.confirm(question, default)

    def check_cancel(self) -> None:
        self.reporter.check_cancel()

    @property
    def cancelled(self) -> bool:
        return self.reporter.cancelled

    # ------------------------------------------------------------ 网络代理
    def session(self, referer: Optional[str] = None, host: Optional[str] = None):
        return make_session(referer=referer, host=host, cookies=self.account.cookie_dict())

    def login_session(self):
        return login_session(self.account.login_key, self.account.login_auth, self.reporter)

    def headers(self, extra: Optional[dict] = None) -> dict:
        return get_headers(extra)

    def path(self, *parts: str) -> str:
        return os.path.join(self.base_dir, *parts)

    def makedirs(self, *parts: str) -> str:
        target = self.path(*parts)
        safe_makedirs(target)
        return target


class BaseTask:
    """一个抓取任务。

    子类需要提供：

    * ``tid``：机器可读 id，也是配置文件里的键；
    * ``name`` / ``summary``：GUI 列表里的标题与一句话说明；
    * ``config_class``：参数定义（GUI 与 CLI 都从这里生成界面）；
    * ``run(ctx, cfg)``：真正干活，返回 :class:`TaskResult`；返回 ``None`` 时
      框架会用 ``ctx.stats`` 兜一个出来，所以老任务不改也能正常上报统计。
    """

    tid: str = ""
    name: str = ""
    summary: str = ""
    config_class: type = BaseConfig
    #: GUI 里的排序权重，越小越前
    order: int = 100

    def base_dir_of(self, cfg: BaseConfig) -> str:
        return resolve_base_dir(getattr(cfg, "base_dir", "./dir"))

    def validate(self, cfg: BaseConfig) -> None:
        """默认调用配置自带的校验；子类可追加。"""
        checker = getattr(cfg, "validate", None)
        if callable(checker):
            checker()

    def run(self, ctx: TaskContext, cfg: BaseConfig):  # pragma: no cover - 抽象
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return "<{} {}>".format(type(self).__name__, self.tid)
