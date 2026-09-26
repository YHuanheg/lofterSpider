"""合集下载：把 lofter 合集（连载整本）按章节顺序抓下来。

和参考项目的关系
----------------
``Bueer99/Lofter_Passage_Get`` 解决的是同一个痛点——**网页端看不到合集内容**。
它的做法是 Selenium 打开文章页、点「上一篇/下一篇」逐篇跳。

本项目**没有沿用它的实现**，因为它有三个硬伤：要装 Chrome + ChromeDriver、
没有重试、没有断点续传。取而代之的是 **App 端接口**
``POST api.lofter.com/v1.1/postCollection.api``（``method=getCollectionDetail``）：
每项的 ``post.content`` **就是完整正文 HTML**，一次请求 50 篇正文。

五种「合集从哪来」（``source``）
--------------------------------
==================  ================================================  ==========
``source``          做什么                                            可靠性
==================  =================================================  ==========
``collection_id``   直接给合集 ID，**支持一行一个填多个**               最高，推荐
``article``         给一篇合集内的文章链接，自动反查合集 ID             高（反查失败会给操作指引）
``author``          给作者主页，下载他的全部合集                       高（接口：getCollectionList）
``subscription``    下载我订阅的全部合集（需登录）                     高（接口：subscribeCollection/list）
``follow_links``    顺着文章页的「上一篇/下一篇」逐篇走                 兜底/实验性
==================  =================================================  ==========

``follow_links`` 是唯一需要解析页面导航的路径。保留它是为了在接口不可用时仍有一条路，
但要说清楚：**lofter 文章页的部分区域是 JS 渲染的，导航链接不一定出现在服务端 HTML 里**；
真遇到抓不到，请改用接口模式。

批量语义
--------
多个目标时**逐个下载、互不影响**：某个合集失败只记一笔统计并继续下一个
（参考项目 ``batch_collections.py`` 是裸 ``for`` 无 ``try``，一个失败整批中断——
这是明确没有照搬的地方）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urljoin

import html2text
from lxml.html import etree

from .. import epub as epub_export
from .. import templates
from ..appapi import AppApi, find_collection_id, to_int
from ..config import CollectionConfig
from ..download import download_many, urls_of
from ..errors import ConfigError, LofterError, NetworkError, ParseError
from ..net import extract_blog_id, fetch_bytes, get_text
from ..utils import (detect_img_type, literal_list, pick_best_img_url, sanitize_filename,
                     ts_to_datetime_str)
from .base import BaseTask, TaskContext

__all__ = [
    "CollectionTask", "Target", "parse_item", "parse_photo_links", "sort_records",
    "chapter_filename", "resolve_collection_id", "resolve_targets",
    "md_link_target", "md_link_text", "RE_IMG_SRC",
]

RE_IMG_SRC = re.compile(r'<img[^>]+src\s*=\s*["\']([^"\']+)["\']', re.I)
RE_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
NAV_TEXT_HINTS = ("下一篇", "下一章", "下一节", "下页")
NAV_CLASS_HINTS = ("next", "post-nav", "postnav", "collection-nav")
NAV_TEXT_AVOID = ("上一篇", "上一章", "上一节", "上一页")

CONVERTER_WIDTH = 0  # 不折行：中文段落被硬折行之后就毁了


# ============================================================ Markdown 转义
def md_link_target(path: str) -> str:
    """转义 Markdown 的**链接目标**。

    文件名里的空格、``#``、``(``、``)``、``&`` 都会让 ``[文字](路径)`` 提前断掉，
    所以必须 percent-encode。``safe="/"`` 是为了保留 ``images/xxx.jpg`` 里的斜杠。
    """
    return quote(str(path).replace("\\", "/"), safe="/")


def md_link_text(text: str) -> str:
    """转义 Markdown 的**链接文字**：``[`` ``]`` 必须反斜杠转义，否则链接会错位。"""
    return str(text).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


# ============================================================ 内容转换
def _make_converter(save_format: str) -> html2text.HTML2Text:
    """构造 html2text 转换器。

    ``body_width=0`` 是关键：默认 78 列折行会把中文段落切得七零八落。
    """
    converter = html2text.HTML2Text(bodywidth=CONVERTER_WIDTH)
    converter.ignore_links = False
    converter.ignore_images = False
    converter.ignore_emphasis = False
    converter.protect_links = True
    converter.unicode_snob = True
    converter.skip_internal_links = True
    converter.single_line_break = False
    return converter


def html_to_content(html: str, save_format: str) -> str:
    """把正文 HTML 转成文本 / Markdown。

    txt 模式下把 ``![alt](url)`` 降级成 ``[图片] url``，因为纯文本阅读器里
    Markdown 图片语法没法看。
    """
    if not html:
        return ""
    text = _make_converter(save_format).handle(html)
    if save_format != "md":
        text = RE_MD_IMG.sub(lambda m: "[图片] {}".format(m.group(2)), text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # lofter 作者习惯用「——」画分隔线，转成 Markdown 的分隔线
    text = re.sub(r"(?m)^[—–-]{2,}$", "---", text)
    return text.strip()


def parse_photo_links(raw) -> list[str]:
    """解析 ``photoLinks`` 字段（JSON 字符串 / Python 字面量 / 已是 list，元素是 dict 或 url）。"""
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            data = literal_list(text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    urls: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            url = pick_best_img_url(entry)
        else:
            url = str(entry or "")
        if url and url not in urls:
            urls.append(url)
    return urls


# ============================================================ 记录解析
def parse_item(item: dict, index: int = 0) -> dict:
    """把接口返回的一条 ``items[]`` 归一化成内部记录。"""
    item = item or {}
    post = item.get("post")
    if not isinstance(post, dict):
        post = item if "title" in item or "content" in item else {}
    blog_info = item.get("blogInfo")
    if not isinstance(blog_info, dict):
        blog_info = post.get("blogInfo") if isinstance(post.get("blogInfo"), dict) else {}

    title = str(post.get("title") or "").strip()
    if not title:
        title = str(post.get("noticeLinkTitle") or "").strip()
    if not title:
        title = "无标题" if not index else "无标题_{:03d}".format(index)

    post_type = to_int(post.get("type"), 1)
    content_html = str(post.get("content") or "")
    photo_urls = parse_photo_links(post.get("photoLinks") or post.get("photoLinksJson"))

    # 图片类型（type=2）的正文常常只有配文，图在 photoLinks 里，补到正文前面
    if post_type == 2 and photo_urls:
        prefix = "".join('<p><img src="{}"/></p>\n'.format(u) for u in photo_urls)
        content_html = prefix + content_html

    tags = post.get("tagList")
    if not isinstance(tags, list):
        raw_tags = post.get("tags") or ""
        tags = [t for t in re.split(r"[,，]", str(raw_tags)) if t.strip()]

    return {
        "title": title,
        "content_html": content_html,
        "post_type": post_type,
        "photo_urls": photo_urls,
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "url": str(post.get("blogPageUrl") or post.get("postUrl") or ""),
        "permalink": str(post.get("permalink") or post.get("id") or ""),
        "author": str(blog_info.get("blogNickName") or post.get("blogNickName") or ""),
        "publish_ts": _publish_ts(post),
        "publish_time": _publish_time(post),
    }


def _publish_ts(post: dict) -> int:
    for key in ("publishTime", "postTime", "createTime", "time", "publishTimeMs"):
        value = post.get(key)
        if value in (None, "", 0, "0"):
            continue
        if isinstance(value, str) and "-" in value:
            continue
        ts = to_int(value)
        if ts > 10 ** 11:
            ts //= 1000
        if ts > 10 ** 8:
            return ts
    return 0


def _publish_time(post: dict) -> str:
    ts = _publish_ts(post)
    if ts:
        return ts_to_datetime_str(ts * 1000)
    for key in ("publishTime", "postTime", "createTime", "time"):
        value = post.get(key)
        if isinstance(value, str) and "-" in value:
            return value.strip()[:16]
    return ""


def sort_records(records: list[dict], mode: str) -> list[dict]:
    """按 ``mode`` 排列章节。

    * ``api``：保持接口返回顺序（``order=1``，通常是作者的排版顺序）；
    * ``asc`` / ``desc``：按发布时间排。

    取不到发布时间的记录**一律排在最后**，不参与排序。
    """
    if mode == "api":
        return list(records)
    with_time = [r for r in records if r.get("publish_ts")]
    without = [r for r in records if not r.get("publish_ts")]
    with_time.sort(key=lambda r: r["publish_ts"], reverse=(mode == "desc"))
    return with_time + without


def chapter_filename(index: int, title: str, suffix: str) -> str:
    """``1, "第三章"`` → ``001_第三章.txt``（补零，方便文件管理器排序）。"""
    safe = sanitize_filename(title) or "无标题"
    return "{:03d}_{}{}".format(index, safe[:80], suffix)


# ============================================================ 目标解析
@dataclass
class Target:
    """一个待下载的合集。"""

    collection_id: str
    label: str = ""

    def display(self) -> str:
        return self.label or "合集 {}".format(self.collection_id)


def resolve_collection_id(ctx: TaskContext, cfg: CollectionConfig,
                          api: AppApi) -> tuple[str, str]:
    """把「用户给的文章链接」变成 ``collectionId``，返回 ``(id, 怎么找到的)``。"""
    url = str(cfg.url).strip()
    found = find_collection_id(url)
    if found:
        return found, "从链接参数里解析出来"

    html = ""
    try:
        html = get_text(ctx.session(), url, cookies=ctx.account.cookie_dict(), referer=url)
        found = find_collection_id(html)
        if found:
            return found, "从文章页 HTML 里解析出来"
    except NetworkError as exc:
        ctx.warn("打开文章页失败：{}".format(exc))

    try:
        blog_id = extract_blog_id(html, what="文章所属作者") if html else ""
    except Exception:
        blog_id = ""
    if blog_id:
        post_id = re.sub(r"[?#].*$", "", url).rstrip("/").split("/")[-1]
        try:
            detail = api.post_detail(blog_id, post_id)
            found = find_collection_id(json.dumps(detail, ensure_ascii=False))
            if found:
                return found, "通过文章详情接口反查到"
        except (NetworkError, ParseError) as exc:
            ctx.warn("文章详情接口查询失败：{}".format(exc))

    return "", ""


def resolve_targets(ctx: TaskContext, cfg: CollectionConfig, api: AppApi) -> list[Target]:
    """按 ``source`` 算出这次要下载哪些合集。"""
    if cfg.source == "collection_id":
        return [Target(cid, "合集 ID {}".format(cid)) for cid in cfg.collection_ids]

    if cfg.source == "article":
        collection_id, how = resolve_collection_id(ctx, cfg, api)
        if not collection_id:
            raise ConfigError(_NO_COLLECTION_ID_HINT.format(cfg.url))
        ctx.log("合集 ID：{}（{}）".format(collection_id, how))
        return [Target(collection_id, "合集 ID {}".format(collection_id))]

    if cfg.source == "author":
        domain = cfg.author_domain
        ctx.log("查询作者 {} 的全部合集…".format(domain))
        collections = api.author_collections(domain)
        targets = [Target(c["id"], c["name"] or "合集 {}".format(c["id"])) for c in collections]
        if not targets:
            raise ConfigError(
                "作者 {} 名下没有查到任何合集。\n"
                "请确认：1) 三级域名写对了（形如 xxx.lofter.com 里的 xxx）；"
                "2) 该作者确实建过合集。".format(domain))
        ctx.log("该作者共 {} 个合集".format(len(targets)))
        return targets

    if cfg.source == "subscription":
        if not ctx.account.ready:
            raise ConfigError("读取订阅列表需要登录，请先在「登录信息」里填 login_auth。")
        ctx.log("查询我订阅的合集…")
        collections = api.subscriptions()
        if not collections:
            raise ConfigError(
                "订阅列表是空的。可能是：1) 这个账号没有订阅任何合集；"
                "2) login_auth 不是 App 端的凭证（订阅接口需要 App 侧登录信息）。")
        targets: list[Target] = []
        for entry in collections:
            label = entry["name"] or "合集 {}".format(entry["id"])
            if not entry["valid"]:
                ctx.count("失效订阅跳过")
                if cfg.skip_subscription_invalid:
                    ctx.warn("跳过已失效的订阅合集：{}".format(label))
                    continue
                ctx.warn("订阅合集已失效，仍按配置继续尝试：{}".format(label))
            targets.append(Target(entry["id"], label))
        ctx.log("订阅合集 {} 个，待下载 {} 个".format(len(collections), len(targets)))
        if not targets:
            raise ConfigError("订阅的合集全部已失效，没有可下载的。")
        return targets

    raise ConfigError("未知的合集来源：{}".format(cfg.source))


_NO_COLLECTION_ID_HINT = (
    "没能从「{}」找出它所属的合集 ID。\n"
    "最稳的做法：打开 lofter App → 进这个合集 → 分享 → 复制链接，"
    "链接里 CollectionId 后面那串数字就是合集 ID。\n"
    "拿到后把「合集从哪来」改成 collection_id 填进去；"
    "也可以改成 author 用作者主页一次下载他所有合集。"
)


# ============================================================ 抓取：接口
def fetch_records_api(ctx: TaskContext, cfg: CollectionConfig, api: AppApi,
                      collection_id: str) -> tuple[dict, list[dict]]:
    """走接口把合集元信息 + 全部章节抓下来。"""
    meta = api.collection_meta(collection_id)
    ctx.log("合集：{}（共 {} 章，作者 {}）".format(
        meta["name"] or "(未命名)", meta["post_count"], meta["author"] or "未知"))
    if meta["description"]:
        ctx.debug("合集简介：{}".format(meta["description"][:120]))

    total = meta["post_count"]
    page_size = max(1, int(cfg.page_size))
    pages = api.page_count(total, page_size) if total else 0
    ctx.log("开始拉取章节列表：{} 章，每页 {} 章，约 {} 页".format(
        total or "未知", page_size, pages or "?"))

    records: list[dict] = []
    for page_no, page_items in enumerate(
            api.iter_collection_pages(collection_id, order=1, page_size=page_size,
                                      post_count=total, max_items=cfg.max_chapters), 1):
        for item in page_items:
            records.append(parse_item(item, index=len(records) + 1))
        ctx.log("已拉取第 {} 页，累计 {} 章".format(page_no, len(records)))
        ctx.progress(len(records), total or 0, "拉取章节列表")

    if not records:
        raise ParseError(
            "合集中没有取到任何章节。可能原因：合集是空的、collectionId 不对，"
            "或者该合集已被作者删除 / 设为私密。")
    return meta, records


# ============================================================ 抓取：页面跳转
def _find_next_link(page, current_url: str) -> str:
    """在文章页里找「下一篇」的链接地址。

    这是参考项目 ``Lofter_Passage_Get`` 的思路（顺着导航往下走），但用
    ``lxml`` 静态解析实现，且**只认「下一篇」方向**——参考项目那个版本
    ``a.prev`` 和 ``a.next`` 一起抓，容易反向或原地打转，只能靠 visited 集合兜底。
    """
    candidates: list[tuple[int, str]] = []
    for link in page.xpath("//a[@href]"):
        href = (link.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        if "/post/" not in href:
            continue

        text = "".join(link.itertext()).strip()
        classes = (link.get("class") or "").lower()
        title = (link.get("title") or "").lower()
        if any(word in text for word in NAV_TEXT_AVOID):
            continue

        score = 0
        if any(word in text for word in NAV_TEXT_HINTS):
            score += 5
        if any(word in title for word in NAV_TEXT_HINTS):
            score += 4
        if "next" in classes:
            score += 3
        if any(hint in classes for hint in NAV_CLASS_HINTS):
            score += 2
        if href.startswith("/"):
            score += 1
        if score:
            candidates.append((score, urljoin(current_url, href)))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def fetch_records_follow(ctx: TaskContext, cfg: CollectionConfig) -> tuple[dict, list[dict]]:
    """兜底方案：从一篇文章开始，顺着页面里的「下一篇」逐篇走。**实验性**。"""
    url = str(cfg.url).strip()
    if not url.startswith("http"):
        raise ConfigError("「顺着页面跳转」模式需要一篇合集中的文章链接（http 开头）")

    limit = int(cfg.max_chapters) if cfg.max_chapters else 500
    records: list[dict] = []
    visited: set[str] = set()
    current = url
    author = ""
    title = ""

    while current and len(records) < limit:
        ctx.check_cancel()
        if current in visited:
            ctx.log("遇到已访问过的链接，停止（可能已到合集开头）")
            break
        visited.add(current)

        html = get_text(ctx.session(), current, cookies=ctx.account.cookie_dict(), referer=current)
        page = etree.HTML(html)
        if page is None:
            break

        if not author:
            names = page.xpath("//a[@class='author-name']//text()") or page.xpath("//h1/a//text()")
            author = names[0].strip() if names else ""

        heading = page.xpath("//h1//text()") or page.xpath("//h2//text()")
        page_title = heading[0].strip() if heading else ""
        if not page_title:
            titles = page.xpath("//title//text()")
            page_title = titles[0].split(" - ")[0].strip() if titles else "无标题"
        if not title:
            title = page_title

        template_id = templates.matcher(page)
        body = templates.get_content(page, template_id, page_title, "article")
        records.append(parse_item({
            "post": {"title": page_title, "content": body or html, "type": 1,
                     "blogPageUrl": current},
            "blogInfo": {"blogNickName": author},
        }, index=len(records) + 1))

        ctx.log("已抓第 {} 篇：{}".format(len(records), page_title))
        ctx.progress(len(records), limit, "逐篇抓取")

        next_url = _find_next_link(page, current)
        if not next_url:
            if len(records) == 1:
                ctx.warn(
                    "文章页里没有找到「下一篇」链接。lofter 的这部分可能是 JS 渲染的，"
                    "静态抓取拿不到——请改用「合集从哪来 = 合集 ID」模式"
                    "（在 App 里进合集 → 分享 → 复制链接，取其中的 CollectionId），"
                    "或者改用 author 模式。")
            else:
                ctx.log("没有更多「下一篇」，抓取结束")
            break
        current = next_url
        time.sleep(0.5)

    meta = {"id": "", "name": title or "未命名合集", "post_count": len(records),
            "author": author, "tags": [], "description": ""}
    return meta, records


# ============================================================ 保存
def download_chapter_images(ctx: TaskContext, cfg: CollectionConfig, record: dict,
                            image_dir: str, index: int, body: str = "") -> tuple[dict, dict]:
    """并发下载一章里出现的图片。

    返回 ``(url→相对路径, url→绝对路径)``。**不返回正文**——正文的本地化留到
    :func:`render_chapter` 里做，这样同一章要渲染两种形态（逐章文件 + 合并形态）时
    **图片只下载一次**。

    下载失败的图片不进映射表，于是正文里仍是原始远程链接，同时伴随一条告警；
    绝不因为一张图挂掉整章。

    **并发任务切分**：一图一个任务、单个任务只用自己的 url 和自己的文件名。
    （参考项目在这里把传入的文件名覆盖成了「第 0 张图」的名字，导致第 1 张之后的图
    全部写进同一个文件、而正文引用的文件名永远不存在——不能照搬。）
    """
    urls = urls_of(body, record["content_html"], record["photo_urls"])
    if not urls:
        return {}, {}

    os.makedirs(image_dir, exist_ok=True)
    # 文件名只依赖序号，不依赖下载结果，所以可以先把计划算好再并发
    plan = {url: "{:03d}-{:02d}.{}".format(index, order, detect_img_type(url))
            for order, url in enumerate(urls, 1)}
    referer = record["url"] or None
    cookies = ctx.account.cookie_dict()

    def fetcher(url: str) -> bytes:
        return fetch_bytes(None, url, referer=referer, cookies=cookies)

    report = download_many(urls, fetcher, workers=max(1, int(cfg.img_workers)),
                           reporter=ctx.reporter, desc="下载图片")

    relative: dict = {}
    absolute: dict = {}
    for url, name in plan.items():
        content = report.contents.get(url)
        if content is None:
            ctx.warn("图片下载失败，保留原链接：{}（{}）".format(url, report.errors.get(url, "未知原因")))
            ctx.count("图片失败")
            continue
        path = os.path.join(image_dir, name)
        with open(path, "wb") as fp:
            fp.write(content)
        relative[url] = "images/{}".format(name)
        absolute[url] = path
        ctx.count("图片成功")
    return relative, absolute


def _rewrite_html_images(html: str, relative: dict) -> str:
    """把正文 HTML 里的远程图片地址换成本地相对路径（EPUB 用）。"""
    if not html or not relative:
        return html
    for url, local in relative.items():
        html = html.replace(url, local)
    return html


def render_chapter(cfg: CollectionConfig, record: dict, index: int, total: int, meta: dict,
                   body: str, nav: Optional[dict] = None, merged: bool = False) -> str:
    """把一章渲染成文本。**纯函数**：不下载、不写盘、不发请求。

    :param body: 已经转换过、并且图片链接已经本地化的正文。
    :param merged: 这一章是要拼进**一个合并文件**里的（单个文件模式，或另存完整版）。
        此时标题降一级（书的标题才是 h1）并写成「第 N 章 标题」，
        并且不写「合集：xx（第 N / M 章）」与文件间的上一篇/下一篇导航——
        同一个文件里那些是冗余的，导航链接更是指向不存在的文件。
    """
    markdown = cfg.save_format == "md"
    if merged:
        lines = ["{} 第 {} 章 {}".format("##" if markdown else "", index, record["title"]).strip()]
    else:
        lines = ["{} {}".format("#" if markdown else "",
                                "{}. {}".format(index, record["title"])).strip()]
    lines.append("")
    if record["author"] or meta.get("author"):
        lines.append("作者：{}".format(record["author"] or meta.get("author")))
    if record["publish_time"]:
        lines.append("发布时间：{}".format(record["publish_time"]))
    if record["url"]:
        lines.append("原文链接：{}".format(record["url"]))
    if not merged:
        lines.append("合集：{}（第 {} / {} 章）".format(
            meta.get("name") or "未命名", index, total))
    lines += ["", "---", "", body]

    if cfg.include_tags and record["tags"]:
        lines += ["", "---", "tag：" + "、".join(record["tags"])]

    if cfg.add_nav and nav and not merged:
        lines += ["", "---"]
        lines.extend(_nav_lines(nav, markdown, record, index, total))
    return "\n".join(lines).rstrip() + "\n"


def compose_chapter(ctx: TaskContext, cfg: CollectionConfig, record: dict, index: int,
                    total: int, meta: dict, image_dir: str,
                    nav: Optional[dict] = None) -> tuple[str, Optional[str], str, dict]:
    """下载本章图片并渲染出需要的文本形态。

    返回 ``(逐章文件用的文本, 合并形态的文本或 None, EPUB 用的 HTML, url→绝对路径)``。

    「合并形态」只在真的要合并时才渲染（单个文件模式，或勾了另存完整版），
    避免白算一遍。
    """
    body = html_to_content(record["content_html"], cfg.save_format)
    relative, absolute = download_chapter_images(ctx, cfg, record, image_dir, index, body)
    for url, local in relative.items():
        body = body.replace(url, local)

    html_for_epub = _rewrite_html_images(record["content_html"] or "", relative)
    standalone = render_chapter(cfg, record, index, total, meta, body, nav=nav, merged=False)
    merged_text = None
    if cfg.single_file or cfg.merge_into_one:
        merged_text = render_chapter(cfg, record, index, total, meta, body, nav=None, merged=True)
    return standalone, merged_text, html_for_epub, absolute


def _nav_lines(nav: dict, markdown: bool, record: dict, index: int, total: int) -> list[str]:
    """文末的「上一篇 / 下一篇 / 目录」导航。

    Markdown 模式下写成真正的链接（文件名经过转义）；纯文本模式下只能写文件名，
    因为 txt 不渲染链接。预览用的标题取自 ``nav`` 里预先算好的章节标题。
    """
    out: list[str] = []
    for key, caption in (("prev", "上一篇"), ("next", "下一篇")):
        entry = nav.get(key)
        if not entry:
            continue
        filename, title = entry
        if markdown:
            out.append("{}：[{}]({})".format(caption, md_link_text(title), md_link_target(filename)))
        else:
            out.append("{}：{}".format(caption, filename))
    index_entry = nav.get("index")
    if index_entry:
        if markdown:
            out.append("目录：[合集目录]({})".format(md_link_target(index_entry)))
        else:
            out.append("目录：{}".format(index_entry))
    return out


def save_index(collection_dir: str, cfg: CollectionConfig, meta: dict,
               records: list[dict], filenames: list[str]) -> str:
    """生成合集目录文件：简介 + tag + 章节列表。

    链接目标与链接文字都做转义——标题里带空格、``#``、``()``、``[]`` 时
    未转义的 Markdown 链接会直接断掉。
    """
    markdown = cfg.save_format == "md"
    name = sanitize_filename(meta.get("name") or "未命名合集")
    path = os.path.join(collection_dir, "{} 目录{}".format(name, ".md" if markdown else ".txt"))
    lines = ["{} {}".format("#" if markdown else "==", meta.get("name") or "未命名合集"),
             "",
             "作者：{}".format(meta.get("author") or "未知"),
             "章节数：{}".format(len(records))]
    if meta.get("tags"):
        lines.append("tag：" + "、".join(meta["tags"]))
    if meta.get("description"):
        lines += ["", meta["description"]]
    lines += ["", "---", "", "目录："]
    for index, (record, filename) in enumerate(zip(records, filenames), 1):
        if markdown:
            lines.append("{}. [{}]({})".format(
                index, md_link_text(record["title"]), md_link_target(filename)))
        else:
            lines.append("{}. {}  —— {}".format(index, record["title"], filename))
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    return path


def merged_filename(collection_name: str, cfg: CollectionConfig) -> str:
    """合并文件 / 单文件的文件名。

    规则：``<合集名> <merge_filename><后缀>``，例如 ``我的连载 完整版.txt``。
    ``merge_filename`` 留空时退回「完整版」，所以想把名字换成别的（比如「全本」），
    改「合并版文件名」那一项即可。
    """
    word = sanitize_filename(str(cfg.merge_filename or "完整版")) or "完整版"
    return "{} {}{}".format(collection_name, word, cfg.file_suffix)


def build_merged_text(cfg: CollectionConfig, meta: dict, records: list[dict],
                      filenames: list[str], chapter_texts: dict,
                      collection_dir: str) -> str:
    """把全部章节拼成一份正文（**单个文件模式与「另存完整版」共用同一套规则**）。

    组织顺序：合集头信息 → 内联目录 → 各章正文。

    * 章节内容优先用**本次内存里刚渲染好的**；本次被跳过的章节才回退去读磁盘上的
      章节文件（单个文件模式下没有章节文件，所以它的跳过粒度是整本级，
      见 :func:`save_collection`）。
    * 目录**故意不做可点击锚点**：Markdown 的锚点按标题 slug 生成，各渲染器规则不一致，
      生成出来的链接很容易是坏的；编号列表反而稳定。
    """
    markdown = cfg.save_format == "md"
    lines = ["{} {}".format("#" if markdown else "==", meta.get("name") or "未命名合集"), ""]
    lines.append("作者：{}".format(meta.get("author") or "未知"))
    lines.append("章节数：{}".format(len(records)))
    if meta.get("tags"):
        lines.append("tag：" + "、".join(meta["tags"]))
    if meta.get("description"):
        lines += ["", meta["description"]]
    lines += ["", "---", "", "目录："]
    for index, record in enumerate(records, 1):
        lines.append("{}. {}".format(index, record["title"]))
    lines += ["", "---", ""]

    for index, record in enumerate(records, 1):
        text = chapter_texts.get(index)
        if not text:
            path = os.path.join(collection_dir, filenames[index - 1]) if filenames else ""
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fp:
                    text = fp.read()
        if not text:
            text = "第 {} 章 {}\n\n（本章未下载）\n".format(index, record["title"])
        lines.append(text.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_merged(collection_dir: str, cfg: CollectionConfig, meta: dict, records: list[dict],
                filenames: list[str], chapter_texts: dict) -> str:
    """渲染并写出合并文件，返回实际路径。"""
    collection_name = sanitize_filename(meta.get("name") or "未命名合集")
    path = os.path.join(collection_dir, merged_filename(collection_name, cfg))
    text = build_merged_text(cfg, meta, records, filenames, chapter_texts, collection_dir)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return path


def save_collection(ctx: TaskContext, cfg: CollectionConfig, meta: dict, records: list[dict], *,
                    base_dir: str, batch: bool) -> str:
    """把一个合集的全部章节落到磁盘，返回合集目录。

    三种保存方式（``save_layout`` × ``merge_into_one``）：

    ====================  ==============  ===========================================
    保存方式               另存完整版       产物
    ====================  ==============  ===========================================
    按章节分文件（默认）     关               ``001_第一章.txt``、``002_第二章.txt``…
    按章节分文件            开              上面那些 **+** ``合集名 完整版.txt``
    **单个文件**           （自动忽略）      **只有** ``合集名 完整版.txt``
    ====================  ==============  ===========================================

    两种章节形态的区别：逐章文件里是「独立文件」形态（``1. 标题``，带跨文件的
    上一篇/下一篇导航）；合并文件里是「书的一章」形态（``第 N 章 标题``，
    不带跨文件导航——那些链接在单文件里指向不存在的文件）。
    """
    records = sort_records(records, cfg.sort_mode)
    if cfg.sort_mode != "api":
        ctx.log("已按发布时间重排章节顺序（{}）".format(cfg.chapter_order))

    collection_name = sanitize_filename(meta.get("name") or "未命名合集")
    folder_name = collection_name
    if batch and meta.get("id"):
        # 批量下载时把 ID 带上，避免两个同名合集的章节互相覆盖
        folder_name = "{} [{}]".format(collection_name, meta["id"])
    collection_dir = os.path.join(base_dir, "collection", folder_name)
    image_dir = os.path.join(collection_dir, "images")
    os.makedirs(collection_dir, exist_ok=True)
    ctx.log("输出目录：{}".format(collection_dir))
    ctx.count("章节总数", len(records))

    suffix = cfg.file_suffix
    filenames = [chapter_filename(i, r["title"], suffix)
                 for i, r in enumerate(records, 1)]
    index_filename = "{} 目录{}".format(collection_name, ".md" if cfg.save_format == "md" else ".txt")

    single_file = cfg.single_file
    merged_path = os.path.join(collection_dir, merged_filename(collection_name, cfg))
    if single_file:
        ctx.log("保存方式：单个文件 → {}".format(os.path.basename(merged_path)))
        if cfg.merge_into_one:
            ctx.log("单个文件模式下「另存一个合并的完整版」自动生效，无需勾选")
        # 单个文件模式的断点粒度是「整本」：没有章节文件可对照，
        # 逐章判断存在性没有意义，只看这一个文件在不在。
        if cfg.skip_existing and os.path.exists(merged_path):
            ctx.log("整本已存在，跳过：{}".format(os.path.basename(merged_path)))
            ctx.count("整本跳过")
            return collection_dir
    else:
        ctx.log("保存方式：按章节分文件（共 {} 章）".format(len(records)))

    keyword = str(cfg.title_filter).strip()
    # 导出 EPUB、或单个文件模式，都需要完整正文 → 逐章的「已下载跳过」不适用
    chapter_level_skip = bool(cfg.skip_existing) and not cfg.export_epub and not single_file
    if cfg.skip_existing and cfg.export_epub and not single_file:
        ctx.log("已开启 EPUB 导出，本次忽略「已下载的章节跳过」以保证整本完整")

    epub_chapters: list[dict] = []
    epub_images: list[tuple] = []
    chapter_texts: dict = {}
    for index, (record, filename) in enumerate(zip(records, filenames), 1):
        ctx.check_cancel()
        if keyword and keyword.lower() not in record["title"].lower():
            ctx.debug("跳过（标题不含「{}」）：{}".format(keyword, record["title"]))
            ctx.count("标题过滤跳过")
            continue

        path = os.path.join(collection_dir, filename)
        if chapter_level_skip and os.path.exists(path):
            ctx.debug("已存在，跳过：{}".format(filename))
            ctx.count("已存在跳过")
            continue

        # 只有按章出文件时才需要「上一篇/下一篇」的跨文件导航
        nav = None
        if cfg.write_chapter_files:
            nav = {
                "prev": (filenames[index - 2], records[index - 2]["title"]) if index >= 2 else None,
                "next": (filenames[index], records[index]["title"]) if index < len(records) else None,
                "index": index_filename if cfg.save_index else None,
            }
        try:
            standalone, merged_text, html_for_epub, absolute = compose_chapter(
                ctx, cfg, record, index, len(records), meta, image_dir, nav)
        except (NetworkError, ParseError) as exc:
            ctx.warn("第 {} 章处理失败，跳过：{}".format(index, exc))
            ctx.count("失败章节")
            continue

        if cfg.write_chapter_files:
            with open(path, "w", encoding="utf-8") as fp:
                fp.write(standalone)
            ctx.log("已保存 {} （{} 字）".format(filename, len(standalone)))
        # 合并形态优先：有就用它，合并文件里才能保持「书的一章」那种格式
        chapter_texts[index] = merged_text if merged_text is not None else standalone
        ctx.count("已保存章节")
        if not cfg.write_chapter_files:
            ctx.log("已拼入单文件：第 {} 章 {}（{} 字）".format(
                index, record["title"], len(chapter_texts[index])))
        ctx.progress(index, len(records), "处理章节")

        if cfg.export_epub:
            epub_chapters.append({"title": record["title"], "html": html_for_epub})
            for url, abs_path in absolute.items():
                epub_images.append(("images/{}".format(os.path.basename(abs_path)), abs_path))

    if not chapter_texts:
        # 全部章节都被「已存在」跳过时**不算失败**：只要磁盘上还有章节文件，
        # 合并版照样能拼出来（build_merged_text 会回退去读磁盘）。
        on_disk = [name for name in filenames
                   if os.path.exists(os.path.join(collection_dir, name))]
        if not on_disk:
            ctx.warn("没有可保存的章节（全被标题过滤，或全部处理失败），本次不产出文件")
            ctx.count("空合集")
            return collection_dir
        ctx.log("本次没有新章节（都已存在），直接用磁盘上已有的 {} 章生成合并文件".format(
            len(on_disk)))

    # ---- 合并 / 单文件
    if cfg.write_merged_file:
        path = save_merged(collection_dir, cfg, meta, records, filenames, chapter_texts)
        ctx.log("{}已生成：{}".format(
            "单个文件" if single_file else "完整版", os.path.basename(path)))

    # ---- 附加产物。单个文件模式下这些会破坏「只出一个文件」的语义，故跳过
    if single_file:
        if cfg.save_index or cfg.save_json:
            ctx.log("单个文件模式：目录已内联进正文，本次跳过「合集目录文件」与「原始 JSON」")
    else:
        if cfg.save_index:
            path = save_index(collection_dir, cfg, meta, records, filenames)
            ctx.log("目录已生成：{}".format(os.path.basename(path)))
        if cfg.save_json:
            path = os.path.join(collection_dir, "{}.json".format(collection_name))
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(records, fp, ensure_ascii=False, indent=2)
            ctx.log("原始数据已保存：{}".format(os.path.basename(path)))
    if cfg.export_epub:
        _export_epub(ctx, cfg, meta, collection_dir, collection_name,
                     epub_chapters, epub_images)

    return collection_dir


def _export_epub(ctx: TaskContext, cfg: CollectionConfig, meta: dict, collection_dir: str,
                 collection_name: str, chapters: list[dict], images: list[tuple]) -> None:
    """导出 EPUB；任何失败都只告警，不影响已经落盘的正文。"""
    usable, reason = epub_export.available()
    if not usable:
        ctx.warn("跳过 EPUB 导出：需要额外安装（{}）。原因：{}".format(
            epub_export.PIP_HINT, reason))
        ctx.count("EPUB跳过")
        return
    if not chapters:
        ctx.warn("没有可用于 EPUB 的章节，跳过导出")
        ctx.count("EPUB跳过")
        return

    out_path = os.path.join(collection_dir, "{}.epub".format(collection_name))
    try:
        epub_export.export_epub(
            chapters, out_path,
            title=meta.get("name") or collection_name,
            author=meta.get("author") or "",
            images=images)
    except Exception as exc:  # noqa: BLE001
        # 这里**故意**捕获宽泛异常：ebooklib / lxml 会抛 ParserError 这类
        # 既不是 LofterError 也不是 OSError 的异常。一本书打包失败，
        # 绝不能让已经下好的正文也被拖垮（参考项目那条「EPUB/PDF 生成失败时
        # 保留 markdown」的修补提交，说的就是这件事）。
        import traceback

        ctx.warn("EPUB 导出失败（正文已保存，不影响其它产物）：{}: {}".format(
            type(exc).__name__, exc))
        ctx.debug(traceback.format_exc())
        ctx.count("EPUB失败")
        return
    ctx.log("EPUB 已生成：{}（{} 章）".format(os.path.basename(out_path), len(chapters)))
    ctx.count("EPUB已导出")


# ============================================================ 任务
class CollectionTask(BaseTask):
    tid = "collection"
    name = "下载 lofter 合集（连载整本）"
    summary = ("支持合集 ID（可多个）/ 文章链接 / 作者全部合集 / 我的订阅，"
               "按章节顺序下载，可断点续传、下图片、合并完整版、导出 EPUB")
    config_class = CollectionConfig
    order = 5

    def run(self, ctx: TaskContext, cfg: CollectionConfig):
        cfg.validate()
        base_dir = self.base_dir_of(cfg)
        os.makedirs(base_dir, exist_ok=True)

        api = AppApi(login_auth=ctx.account.login_auth,
                     login_key=ctx.account.login_key,
                     extra_headers=cfg.headers_json,
                     extra_cookies=cfg.cookies_json,
                     verify_ssl=cfg.verify_ssl,
                     max_retries=cfg.max_retries,
                     reporter=ctx.reporter)

        # ---- 阶段1：确定要下载什么
        ctx.stage("阶段1：确定要下载的合集")
        if cfg.source == "follow_links":
            meta, records = fetch_records_follow(ctx, cfg)
            ctx.stage("阶段2：保存章节")
            save_collection(ctx, cfg, meta, records, base_dir=base_dir, batch=False)
        else:
            targets = resolve_targets(ctx, cfg, api)
            ctx.log("共 {} 个合集待下载".format(len(targets)))
            batch = len(targets) > 1
            success = 0
            for position, target in enumerate(targets, 1):
                ctx.check_cancel()
                ctx.stage("合集 {}/{}：{}".format(position, len(targets), target.display()))
                try:
                    meta, records = fetch_records_api(ctx, cfg, api, target.collection_id)
                    save_collection(ctx, cfg, meta, records, base_dir=base_dir, batch=batch)
                    success += 1
                    ctx.count("成功合集")
                except (LofterError, OSError) as exc:
                    # 逐个隔离：一个合集失败绝不能中断后面的（参考项目就是裸 for 中断整批）
                    ctx.count("失败合集")
                    ctx.warn("合集「{}」处理失败，继续下一个：{}".format(target.display(), exc))
                    if not batch:
                        raise
            ctx.set_stat("待下载合集", len(targets))
            if not batch and success == 0:
                raise LofterError("合集下载失败，请查看上面的日志")

        result = ctx.make_result(ok=True, message="合集下载完成", output_dir=base_dir)
        result.task = self.tid
        ctx.log("完成：{}".format(result.summary()))
        return result
