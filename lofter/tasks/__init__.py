"""任务注册表。

新增一个任务只需要：写一个 ``BaseTask`` 子类 → 在这里加进 ``TASK_CLASSES``。
GUI 的任务列表、CLI 的 ``--task`` 取值、配置文件的 key 都会自动跟着变。
"""

from __future__ import annotations

import time
from typing import Optional

from ..config import Account, CONFIG_CLASSES, load_account, load_task_configs
from ..errors import ConfigError
from ..reporter import Reporter
from .base import BaseTask, TaskContext, TaskResult
from .author_img import AuthorImgTask
from .author_txt import AuthorTxtTask
from .blogs import BlogsTask
from .collection import CollectionTask
from .homepage import HomepageTask
from .like_share_tag import LikeShareTagTask
from .phone_tag import PhoneTagTask

__all__ = [
    "BaseTask",
    "TaskContext",
    "TaskResult",
    "TASK_CLASSES",
    "TASKS",
    "TASK_BY_ID",
    "get_task",
    "run_task",
    "default_config",
    "load_config",
]

TASK_CLASSES = (
    CollectionTask,
    LikeShareTagTask,
    AuthorImgTask,
    AuthorTxtTask,
    BlogsTask,
    HomepageTask,
    PhoneTagTask,
)

TASKS: list[BaseTask] = sorted((cls() for cls in TASK_CLASSES), key=lambda task: task.order)
TASK_BY_ID = {task.tid: task for task in TASKS}


def get_task(tid: str) -> BaseTask:
    try:
        return TASK_BY_ID[tid]
    except KeyError as exc:
        raise ConfigError("未知任务 {}，可选：{}".format(tid, " / ".join(TASK_BY_ID))) from exc


def default_config(tid: str):
    """取某任务的默认配置。"""
    return get_task(tid).config_class()


def load_config(tid: str, configs: Optional[dict] = None):
    """从 ``config/settings.json`` 取某任务的配置（缺省值兜底）。"""
    configs = configs if configs is not None else load_task_configs()
    return configs.get(tid) or default_config(tid)


def run_task(tid: str, cfg, account: Optional[Account] = None,
             reporter: Optional[Reporter] = None,
             base_dir: Optional[str] = None) -> TaskResult:
    """跑一个任务，返回 :class:`TaskResult`。

    任务自己可以 ``return ctx.make_result(...)``；如果它返回 ``None``（老的写法），
    这里用 ``ctx.stats`` 兜一个出来，所以新增统计能力不需要改动既有任务。

    :param base_dir: 覆盖输出目录；不传就用 ``cfg.base_dir`` 经任务自己解析后的路径。
    """
    from ..reporter import CliReporter

    task = get_task(tid)
    account = account or load_account()
    reporter = reporter or CliReporter()

    if tid in CONFIG_CLASSES and not isinstance(cfg, CONFIG_CLASSES[tid]):
        cfg = CONFIG_CLASSES[tid].from_dict(
            cfg.to_dict() if hasattr(cfg, "to_dict") else dict(cfg))

    target_dir = base_dir or task.base_dir_of(cfg)
    account.check()
    task.validate(cfg)

    ctx = TaskContext(account, reporter, target_dir)
    reporter.log("[{}] 输出目录：{}".format(task.name, target_dir))

    started = time.time()
    result = task.run(ctx, cfg)

    if not isinstance(result, TaskResult):
        result = ctx.make_result()
    if not result.task:
        result.task = tid
    if not result.elapsed:
        result.elapsed = time.time() - started
    if not result.output_dir:
        result.output_dir = target_dir
    return result
