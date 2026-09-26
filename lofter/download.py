"""并发下载辅助。

参考 `lofter-getter` 用 ``ThreadPoolExecutor`` 并发下载图片的做法，但**修正了它的任务切分缺陷**：

它的 ``download_single_image(img_url, img_name, ...)`` 函数体里第一行又写了一遍
``for i, img in enumerate(img_url_list):``，把传进来的 ``img_name`` 覆盖成 ``img_name_list[0]``，
并在第一轮就 ``return``。结果是每个任务都把自己的图片写进「第 0 张图」的文件名里，
第 1 张之后全部落到同一个 ``_0.jpg`` 上互相覆盖 —— 而 Markdown 里引用的
``images/<标题>_1.jpg`` 永远不会被创建。

本模块的契约非常明确：

* **一个 url 一个任务**，任务只使用自己的 url 与自己的返回值；
* :attr:`DownloadReport.calls` 记录**实际发起的下载次数**，
  测试用 ``report.calls == len(urls)`` 把「一个任务偷下多张图」这类回归钉死；
* 并发数**可配**且默认保守（3），因为图床对并发敏感；
* 每次拿到结果都检查取消标志，保证「取消」在并发场景下依然有效。
"""

from __future__ import annotations

import concurrent.futures as futures
from dataclasses import dataclass, field as dc_field
from typing import Callable, Iterable, Optional

from .errors import TaskCancelled

__all__ = ["DownloadReport", "download_many"]

DEFAULT_WORKERS = 3


@dataclass
class DownloadReport:
    """一次批量下载的结果。"""

    #: url -> 二进制内容（成功的）
    contents: dict = dc_field(default_factory=dict)
    #: url -> 错误描述（失败的）；失败不影响其他项
    errors: dict = dc_field(default_factory=dict)
    #: 实际发起的下载次数。用于防「一个任务下多张图」的回归
    calls: int = 0

    @property
    def ok_count(self) -> int:
        return len(self.contents)

    @property
    def failed_count(self) -> int:
        return len(self.errors)

    @property
    def total(self) -> int:
        return self.ok_count + self.failed_count


def download_many(urls: Iterable[str], fetcher: Callable[[str], bytes], *,
                  workers: int = DEFAULT_WORKERS,
                  reporter=None,
                  desc: str = "下载") -> DownloadReport:
    """并发下载一组 url。

    :param fetcher: ``(url) -> bytes``。抛异常即视为该 url 失败。
        由调用方决定超时、Referer、cookie 等细节，因此这里不依赖 requests。
    :param workers: 并发数，会夹到 ``[1, len(urls)]``。
    :param reporter: 传入 :class:`~lofter.reporter.Reporter` 可上报进度并支持取消。

    **取消语义**：一旦取消，尚未开始的任务会被丢弃、已完成的照常返回，
    然后抛出 :class:`~lofter.errors.TaskCancelled`（由上层决定怎么收尾）。
    """
    unique = list(dict.fromkeys(url for url in urls if url))
    report = DownloadReport()
    if not unique:
        return report

    pool_size = max(1, min(int(workers or DEFAULT_WORKERS), len(unique)))
    total = len(unique)
    if reporter is not None:
        reporter.debug("并发下载 {} 项，并发数 {}".format(total, pool_size))

    pool = futures.ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="lofter-dl")
    try:
        submitted = {pool.submit(_fetch_one, fetcher, url): url for url in unique}
        try:
            for index, future in enumerate(futures.as_completed(submitted), 1):
                url = submitted[future]
                report.calls += 1
                error, content = future.result()
                if error is None:
                    report.contents[url] = content
                else:
                    report.errors[url] = error
                if reporter is not None:
                    reporter.progress(index, total, desc)
                    reporter.check_cancel()
        except TaskCancelled:
            for future in submitted:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    finally:
        # 正常路径要等线程收干净；取消路径上面已经 shutdown 过，这里重复调用是幂等的
        pool.shutdown(wait=True)
    return report


def _fetch_one(fetcher: Callable[[str], bytes], url: str):
    """返回 ``(错误描述, 内容)``，两者必有一个为 None。绝不向上抛异常。"""
    try:
        return None, fetcher(url)
    except Exception as exc:  # noqa: BLE001 - 单张图失败不该影响整批
        return "{}: {}".format(type(exc).__name__, exc), None


def urls_of(record_text: str, record_html: str, extra: Optional[list] = None) -> list[str]:
    """把「一篇内容里出现过的图片 url」按出现顺序去重收集起来。

    同时扫正文 HTML 的 ``<img src>`` 与额外的图片列表（例如 App 接口的
    ``photoLinks``），因为 lofter 的图片型博客正文里可能一张 ``<img>`` 都没有。
    """
    import re

    found: list[str] = list(extra or [])
    found += re.findall(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', record_html or "", re.I)
    if record_text:
        found += re.findall(r"!\[[^\]]*\]\(([^)\s]+)", record_text)
    return list(dict.fromkeys(url for url in found if url))
