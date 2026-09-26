"""l15：手机端 tag 接口（原 ``l15_phone_tag.py``）。

原脚本自己标注「**未完成、不能直接使**」——它只验证了 tag 列表接口能拿到数据，
博客详情只是把 json 存下来，没有正文提取。

这里保留全部原有能力，并明确标成**实验性**：

* 走 ``api.lofter.com``（App 端），需要 App 侧的设备头与 App cookie，PC 端 cookie 不通用；
* 接口有个隐含约束：**一个 ``NTESwebSI`` 只能用于一组 tag 页请求**，每轮翻页前要刷新；
* 部分网络环境下 ``api.lofter.com`` 的证书校验不过，此时把「校验 SSL 证书」关掉。

**只是想抓 tag 的话，用「保存喜欢 / 推荐 / tag」任务的 tag 模式就好**，那个是能直接用的。
本任务的价值在于：手机端热榜的条数上限比 PC 端高得多（1000 条 vs 500 条），
而且能按月份翻历史热榜。
"""

from __future__ import annotations

import json
import os
import time

from ..appapi import AppApi
from ..config import PhoneTagConfig
from ..errors import NetworkError, ParseError
from .base import BaseTask, TaskContext

__all__ = ["PhoneTagTask"]

MAX_PAGES = 100          # 服务端上限：翻到 100 页之后开始返回空
DETAIL_LIMIT = 200       # 详情抓取上限，避免实验性任务跑失控


class PhoneTagTask(BaseTask):
    tid = "phone_tag"
    name = "手机端 tag 抓取（实验性）"
    summary = ("走 App 接口抓 tag 榜单（上限比 PC 端高），需要 App 侧凭证；"
               "正文提取未实现，抓到的是原始 json")
    config_class = PhoneTagConfig
    order = 90

    def run(self, ctx: TaskContext, cfg: PhoneTagConfig):
        cfg.validate()
        base_dir = self.base_dir_of(cfg)
        os.makedirs(base_dir, exist_ok=True)

        api = AppApi(login_auth=ctx.account.login_auth,
                     login_key=ctx.account.login_key,
                     extra_headers=cfg.headers_json,
                     extra_cookies=cfg.cookies_json,
                     verify_ssl=cfg.verify_ssl,
                     reporter=ctx.reporter)

        # ---- 阶段1：抓 tag 列表
        ctx.stage("阶段1：抓 tag 列表")
        ctx.log("tag={}  榜单={}  月份={}  类型={}".format(
            cfg.tag, cfg.list_type, cfg.timelimit or "不限", cfg.blog_type or "全部"))

        tag_data: list[dict] = []
        permalinks: set[str] = set()
        offset = 0
        page = 0
        while page < MAX_PAGES:
            ctx.check_cancel()
            api.refresh_si()
            payload = api.tag_posts(cfg.tag, list_type=cfg.list_type, offset=offset,
                                    post_types=cfg.blog_type, timelimit=cfg.timelimit)

            offset = int(payload.get("offset") or 0)
            items = payload.get("list")
            if not isinstance(items, list):
                items = []

            # 翻完（tag 总量不足 100 页）服务端会从头再来一遍，用 permalink 去重判断
            first_link = ""
            if items:
                first_link = str((((items[0].get("postData") or {}).get("postView")) or {})
                                 .get("permalink") or "")
            if not items or (first_link and first_link in permalinks):
                ctx.log("tag 页信息获取结束")
                break

            tag_data.extend(items)
            for item in items:
                link = str((((item.get("postData") or {}).get("postView")) or {})
                           .get("permalink") or "")
                if link:
                    permalinks.add(link)
            page += 1
            ctx.count("已抓取条目", len(items))
            ctx.log("第 {} 页 {} 条，累计 {} 条".format(page, len(items), len(tag_data)))
            ctx.progress(page, MAX_PAGES, "抓取 tag 列表")
            time.sleep(0.5)

        if not tag_data:
            raise ParseError(
                "一条都没抓到。请检查：\n"
                "1) tag 名是否正确；\n"
                "2) 本任务走的是 App 接口，需要 App 侧的请求头 / cookie，PC 端 cookie 不通用；\n"
                "3) 试试把「校验 SSL 证书」关掉再跑。")

        out_path = os.path.join(base_dir, "tag_data.json")
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(tag_data, fp, ensure_ascii=False, indent=4)
        ctx.set_stat("榜单条目", len(tag_data))
        ctx.log("tag 列表已写入 {}（{} 条）".format(out_path, len(tag_data)))

        # ---- 阶段2：抓博客详情
        ctx.stage("阶段2：抓博客详情（实验性）")
        detail_dir = os.path.join(base_dir, "blog_detail")
        os.makedirs(detail_dir, exist_ok=True)
        limit = min(len(tag_data), DETAIL_LIMIT)
        saved = 0
        for index, item in enumerate(tag_data[:limit], 1):
            ctx.check_cancel()
            if self._save_detail(ctx, api, item, detail_dir):
                saved += 1
            ctx.progress(index, limit, "抓博客详情")
        ctx.count("详情已保存", saved)

        result = ctx.make_result(
            ok=True,
            message="注意：正文提取尚未实现，抓到的是原始 json",
            output_dir=base_dir)
        result.task = self.tid
        ctx.log("完成：{}".format(result.summary()))
        return result

    @staticmethod
    def _save_detail(ctx: TaskContext, api: AppApi, item: dict, out_dir: str) -> bool:
        """抓一篇博客的详情并存成 json。失败只告警不中断。"""
        post_view = (item.get("postData") or {}).get("postView") or {}
        blog_info = item.get("blogInfo") or {}
        permalink = str(post_view.get("permalink") or "")
        blog_id = str(blog_info.get("blogId") or "")
        post_id = str(post_view.get("id") or "")
        if not (blog_id and post_id):
            ctx.debug("条目缺少 blogId/postId，跳过：{}".format(permalink))
            return False

        try:
            payload = api.post_detail_json(blog_id, post_id)
        except (NetworkError, ParseError) as exc:
            ctx.warn("抓详情失败 {}：{}".format(permalink or post_id, exc))
            return False

        name = "{}.json".format(permalink or post_id)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        ctx.debug("详情已保存：{}".format(name))
        return True
