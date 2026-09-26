"""l8 + l10 合并：按链接列表保存单篇博客（原 ``l8_blogs_img.py`` / ``l10_blogs_txt.py``）。

两个脚本的使用方式完全一样——「把链接一行一个写进 dir/img_list 或 dir/txt_list，然后运行」，
代码也高度重合，所以合并成一个任务，用 ``kind`` 区分：

* ``kind = "img"``：只把每篇博客里的图片存下来 → ``dir/img/this/``
* ``kind = "txt"``：把每篇博客的正文抽出来存 txt → ``dir/article/this/``

发表时间、标题都要**回归档页查**（博客页自身不一定显示完整时间），而仅自己可见的内容
在归档页里查不到，此时退回从博客页正则猜时间——这两个分支都保留。
"""

from __future__ import annotations

import os
import re

from lxml.html import etree

from .. import templates
from ..archive import (ENTRY_PATTERN_IMG, ENTRY_PATTERN_TXT, make_archive_data,
                       make_archive_head)
from ..config import BlogsConfig
from ..errors import ConfigError, NetworkError
from ..net import extract_blog_id, fetch_bytes, get_text, post_dwr
from ..utils import (dedup_filename, detect_img_type, filter_img_urls, js_unescape_latin,
                     sanitize_filename)
from .base import BaseTask, TaskContext

__all__ = ["BlogsTask"]

RE_IMG_NEW = re.compile(r'"(http[s]{0,1}://imglf\d{0,1}.lf\d*.[0-9]{0,3}.net.*?)"')
RE_DATE = re.compile(r"\d{4}[.\\/-]\d{2}[.\\/-]\d{2}")
RE_AUTHOR_IP = re.compile(r"http(s)*://(.*).lofter.com/")
RE_TIME = re.compile(r"s[\d]*.time=(\d*);")
RE_ENTRY_TITLE = re.compile(r'[\d]*.title="(.*?)"')
RE_LAST_TIME = re.compile(r"s%d\.time=(.*);s.*type")


def blog_author_url(blog_url: str) -> str:
    """``https://x.lofter.com/post/abc`` → ``https://x.lofter.com/``。"""
    return blog_url.split("/post")[0].rstrip("/") + "/"


def find_archive_entry(ctx: TaskContext, blog_url: str, author_id: str, kind: str):
    """在归档页里翻到这篇博客对应的原始条目，找不到返回 ``None``。

    找不到通常意味着这篇是**仅自己可见**（归档页不含私密内容），
    或者博客已被删除。
    """
    author_url = blog_author_url(blog_url)
    archive_url = author_url + "dwr/call/plaincall/ArchiveBean.getArchivePostByTime.dwr"
    data = make_archive_data(author_id, 50)
    headers = make_archive_head(author_url)
    blog_id = blog_url.split("/")[-1]
    pattern = ENTRY_PATTERN_IMG if kind == "img" else ENTRY_PATTERN_TXT

    session = ctx.session(host=author_url.split("//")[1].rstrip("/"), referer=author_url + "view")
    session.headers.update(headers)

    while True:
        ctx.check_cancel()
        page_data = post_dwr(session, archive_url, data)
        for entry in pattern.findall(page_data):
            if blog_id in entry:
                return entry
        # 最后一页条目数不足 50 条时，取“第 49 条时间”的正则会匹配失败
        match = RE_LAST_TIME.search(page_data)
        if not match:
            return None
        data["c0-param2"] = "number:" + str(match.group(1))
        from ..net import polite_sleep

        polite_sleep(1, 2, ctx.reporter)


def resolve_blog_meta(ctx: TaskContext, blog_url: str,
                      want_title: bool) -> tuple[str, str, str, str]:
    """返回 ``(author_name, author_id, author_ip, public_time, title)``。

    ``want_title`` 为 True 时（txt 分支）会顺带从归档条目里取标题。
    """
    author_url = blog_author_url(blog_url)
    view_html = get_text(ctx.session(), author_url + "view",
                         cookies=ctx.account.cookie_dict(), referer=author_url + "view")
    view = etree.HTML(view_html)
    author_id = extract_blog_id(view_html, what="作者归档页")
    ctx.check_cancel()

    names = view.xpath("//h1/a/text()")
    author_name = names[0] if names else author_url.split("//")[1].split(".lofter.com")[0]

    m = RE_AUTHOR_IP.search(blog_url)
    author_ip = m.group(2) if m else ""

    public_time, title = "", ""
    try:
        entry = find_archive_entry(ctx, blog_url, author_id, "txt" if want_title else "img")
    except (NetworkError, AttributeError, ValueError) as exc:
        ctx.debug("归档页查询失败：{}".format(exc))
        entry = None

    if entry:
        ts = RE_TIME.search(entry)
        if ts:
            import time as _time

            public_time = _time.strftime("%Y-%m-%d",
                                        _time.localtime(int(int(ts.group(1)) / 1000)))
        if want_title:
            titles = RE_ENTRY_TITLE.findall(entry)
            if titles:
                # 文本博客的 title 是「作者名 日期」这种临时标题，也会走到这里
                title = js_unescape_latin(titles[0])
            else:
                # 图片配文没有 title 字段
                title = "图片配文 {}".format(public_time)
    return author_name, author_id, author_ip, public_time, title


# =============================================================== kind = img
def save_images(ctx: TaskContext, blog_url: str, out_dir: str) -> int:
    author_name, _a, author_ip, public_time, _t = resolve_blog_meta(ctx, blog_url, want_title=False)
    if not public_time:
        public_time = "获取发表时间失败"

    html = get_text(ctx.session(), blog_url, cookies=ctx.account.cookie_dict(), referer=blog_url)
    imgs_url = RE_IMG_NEW.findall(html)
    if not filter_img_urls(imgs_url, "img"):
        ctx.debug("新格式正则没匹配到，尝试旧格式")
    imgs_url = filter_img_urls(imgs_url, "img")
    if not imgs_url:
        ctx.warn("这篇博客没抓到图片：{}".format(blog_url))
        return 0

    safe_name = sanitize_filename(author_name)
    saved = 0
    for index, img_url in enumerate(imgs_url, 1):
        ctx.check_cancel()
        img_type = detect_img_type(img_url)
        pic_name = "{}[{}] {}({}).{}".format(safe_name, author_ip, public_time, index, img_type)
        try:
            content = fetch_bytes(None, img_url, referer=blog_url)
        except NetworkError as exc:
            ctx.warn("图片下载失败：{}".format(exc))
            continue
        with open(os.path.join(out_dir, sanitize_filename(pic_name)), "wb") as fp:
            fp.write(content)
        saved += 1
        ctx.log("图片已保存 {} （{}/{}）".format(pic_name, index, len(imgs_url)))
    return saved


# =============================================================== kind = txt
def save_text(ctx: TaskContext, blog_url: str, out_dir: str) -> int:
    author_name, _a, author_ip, public_time, title = resolve_blog_meta(ctx, blog_url,
                                                                      want_title=True)
    html = get_text(ctx.session(), blog_url, cookies=ctx.account.cookie_dict(), referer=blog_url)
    parse = etree.HTML(html)

    # 归档页查不到（多半是仅自己可见）时，退回从博客页猜
    if not public_time and not title:
        ctx.log("归档页没有这篇，尝试从博客页匹配标题与时间")
        nodes = parse.xpath("//h2//text()")
        if nodes:
            title = nodes[0]
        m = RE_DATE.search(html)
        if m:
            public_time = m.group(0).replace("\\", "-").replace(".", "-").replace("/", "-")
        else:
            public_time = "1970-01-01"
            ctx.warn("时间匹配失败，按 1970-01-01 保存")

    blog_type = "article" if title else "text"
    head = "{} by {}[{}]\n发表时间：{}\n原文链接： {}".format(
        title, author_name, author_ip, public_time, blog_url)

    template_id = templates.matcher(parse)
    ctx.log("文字匹配模板为模板 {}".format(template_id))
    if template_id == templates.TEMPLATE_GENERIC:
        ctx.warn("使用了通用模板 0，正文外可能掺入别的内容")
    body = templates.get_content(parse, template_id, title, blog_type)
    article = head + "\n\n\n\n" + body

    file_name = ("{} by {}.txt".format(title, author_name) if title
                 else "{} {}.txt".format(author_name, public_time))
    file_name = sanitize_filename(file_name)
    file_name = dedup_filename(file_name, article, out_dir, "txt")
    with open(os.path.join(out_dir, file_name), "w", encoding="utf-8") as fp:
        fp.write(article)
    ctx.log("{} 保存完成".format(file_name))
    return 1


class BlogsTask(BaseTask):
    tid = "blogs"
    name = "单篇保存（按链接列表）"
    summary = "照 dir/img_list 或 dir/txt_list 里的链接逐篇保存，配合主页扫描可存仅自己可见的内容"
    config_class = BlogsConfig
    order = 40

    def run(self, ctx: TaskContext, cfg: BlogsConfig):
        cfg.validate()
        base_dir = self.base_dir_of(cfg)

        if cfg.source == "file":
            list_path = os.path.join(base_dir, cfg.list_file_name)
            if not os.path.exists(list_path):
                raise ConfigError(
                    "找不到链接列表文件：{}\n"
                    "请在输出目录里新建它、一行写一个博客链接；"
                    "或把「链接来源」改成 custom 直接填链接。".format(list_path))
            with open(list_path, "r", encoding="utf-8") as fp:
                blog_urls = [line.strip() for line in fp if line.strip()]
        else:
            blog_urls = [u.strip() for u in cfg.urls if u.strip()]

        if not blog_urls:
            raise ConfigError("链接列表是空的")

        out_dir = os.path.join(base_dir, "img" if cfg.kind == "img" else "article", "this")
        os.makedirs(out_dir, exist_ok=True)
        ctx.log("共 {} 个链接，输出到 {}".format(len(blog_urls), out_dir))

        worker = save_images if cfg.kind == "img" else save_text
        failed = 0
        for index, blog_url in enumerate(blog_urls, 1):
            ctx.check_cancel()
            ctx.stage("第 {}/{} 篇".format(index, len(blog_urls)))
            try:
                worker(ctx, blog_url, out_dir)
            except (NetworkError, ConfigError) as exc:
                failed += 1
                ctx.warn("处理失败，跳过：{} —— {}".format(blog_url, exc))
            ctx.progress(index, len(blog_urls), "已处理 {} 篇".format(index))

        ctx.log("全部完成：成功 {} 篇，失败 {} 篇".format(len(blog_urls) - failed, failed))
