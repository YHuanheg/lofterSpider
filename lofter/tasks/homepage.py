"""l14：主页扫描，抠出所有博客链接（原 ``l14_default_homepage_extract.py``）。

它的价值是**能拿到仅自己可见的博客链接**——lofter 默认模板（灰色良品）的主页
会把私密内容也渲染出来，而归档页不会。拿到链接后再交给「单篇保存」即可。

**修 bug**：原 ``homepage_extract(homepage_url, page)`` 内部构造 Host 头时用的是
**全局变量 ``url``** 而不是参数 ``homepage_url``，只是碰巧在 ``__main__`` 里两者同名才没出事。
这里改成用参数，否则 GUI 里换个链接就会算错 Host。
"""

from __future__ import annotations

import os
import time

from lxml.html import etree

from ..config import HomepageConfig
from ..errors import ConfigError, NetworkError
from ..net import get_text
from .base import BaseTask, TaskContext

__all__ = ["HomepageTask"]

HEADERS_TEMPLATE = {
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
              "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8,en;q=0.7",
}


def homepage_extract(ctx: TaskContext, homepage_url: str, page: int) -> list[str]:
    """取主页第 ``page`` 页里的博客链接。"""
    host = homepage_url.split("//")[1].rstrip("/")
    headers = dict(HEADERS_TEMPLATE)
    headers["Host"] = host
    headers["Referer"] = homepage_url

    session = ctx.session()
    try:
        resp = session.get(homepage_url, headers=headers, params={"page": str(page)},
                           cookies=ctx.account.cookie_dict(), timeout=30)
    except Exception as exc:
        raise NetworkError("扫描主页第 {} 页失败：{}".format(page, exc)) from exc

    html = resp.content.decode("utf-8", errors="replace")
    parse = etree.HTML(html)
    return parse.xpath("//div[contains(@class,'postwrapper')]/div//div[@class='day']/a/@href")


class HomepageTask(BaseTask):
    tid = "homepage"
    name = "主页扫描（含仅自己可见）"
    summary = "翻 lofter 默认模板主页，把所有博客链接导成 links.txt，供「单篇保存」使用"
    config_class = HomepageConfig
    order = 50

    def run(self, ctx: TaskContext, cfg: HomepageConfig):
        cfg.validate()
        base_dir = self.base_dir_of(cfg)
        os.makedirs(base_dir, exist_ok=True)
        out_path = cfg.output if os.path.isabs(cfg.output) else os.path.join(base_dir, cfg.output)

        all_links: list[str] = []
        page = 1
        while True:
            ctx.check_cancel()
            links = homepage_extract(ctx, cfg.url, page)
            if not links:
                break
            ctx.log("第 {} 页 链接数 {}".format(page, len(links)))
            all_links += links
            ctx.progress(page, 0, "已扫 {} 页，{} 个链接".format(page, len(all_links)))
            page += 1
            time.sleep(1.5)

        if not all_links:
            raise ConfigError(
                "一个链接都没扫到。请确认：\n"
                "1) 主页用的是 lofter 默认模板（灰色良品）；\n"
                "2) login_auth 有效；\n"
                "3) 链接结尾带 /。")

        ctx.log("共 {} 个链接，去重后 {} 个".format(len(all_links), len(set(all_links))))
        with open(out_path, "w", encoding="utf-8") as fp:
            for link in all_links:
                fp.write(link + "\n")
        ctx.log("已写入 {}".format(out_path))
