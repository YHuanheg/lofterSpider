"""l4：保存某个作者主页的所有图片（原 ``l4_author_img.py``）。

走**归档页**（ArchiveBean）拿博客列表 → 逐篇打开博客页用正则抠图片链接 → 下载。

两阶段都支持断点：

* 阶段A（解析博客页拿到图片链接）进度在 ``author_img_file/blogs_info.json``
  与 ``imgs_info.json``，跑完写 ``"finished"``；
* 阶段B（下载图片）进度在 ``imgs_info.json`` 与 ``imgs_info_saved.json``。

> 注意：归档页**不显示仅自己可见**的内容。要存自己主页的私密内容，
> 请用「主页扫描」+「单篇保存」的组合（README 有说明）。
"""

from __future__ import annotations

import os
import re

from lxml.html import etree

from ..archive import ENTRY_PATTERN_IMG, fetch_archive_entries, parse_archive_img_entries
from ..config import AuthorImgConfig
from ..errors import ConfigError, NetworkError
from ..net import extract_blog_id, fetch_bytes, get_text, polite_sleep
from ..progress import read_json, read_text, write_json
from ..utils import detect_img_type, filter_img_urls, sanitize_filename, tag_match
from .base import BaseTask, TaskContext

__all__ = ["AuthorImgTask"]

SUBDIR = "author_img_file"
BLOGS_FILE = "blogs_info.json"
IMGS_FILE = "imgs_info.json"
PARSED_FILE = "blogs_info_parsed.json"
SAVED_FILE = "imgs_info_saved.json"
FINISHED = "finished"

RE_IMG_NEW = re.compile(r'"(http[s]{0,1}://imglf\d{0,1}.lf\d*.[0-9]{0,3}.net.*?)"')
RE_IMG_OLD = re.compile(r'"(http[s]{0,1}://imglf\d.nosdn\d*.[0-9]{0,3}\d.net.*?)"')


def _first_line_or_empty(path: str) -> str:
    """原 ``is_file_in``。**修 bug**：原来 ``open(path, "r")`` 没指定编码，
    Windows 上会按 GBK 读，遇到写进去的 UTF-8 中文就出问题。"""
    if not os.path.exists(path):
        return ""
    line = read_text(path).split("\n", 1)[0].replace("\r", "")
    return line


def _state_paths(file_path: str) -> dict:
    base = os.path.join(file_path, SUBDIR)
    return {name: os.path.join(base, name)
            for name in (BLOGS_FILE, IMGS_FILE, PARSED_FILE, SAVED_FILE)}


def init_state_files(file_path: str, ctx: TaskContext) -> dict:
    """原 ``deal_file("init")``：准备好四个进度文件。"""
    base = os.path.join(file_path, SUBDIR)
    paths = _state_paths(file_path)
    ctx.log("检查运行所需的文件")
    if not os.path.exists(base):
        os.makedirs(base, exist_ok=True)
        ctx.log("创建文件夹：{}".format(base))
    if not os.path.exists(paths[BLOGS_FILE]):
        _write_text(paths[BLOGS_FILE], "")
    for name in (IMGS_FILE, PARSED_FILE, SAVED_FILE):
        if not os.path.exists(paths[name]):
            _write_text(paths[name], "[]")
    return paths


def cleanup_state_files(paths: dict, ctx: TaskContext) -> None:
    """原 ``deal_file("del")``：跑完删掉进度文件（保持原行为，便于下次全量重跑）。"""
    for name, path in paths.items():
        if os.path.exists(path):
            try:
                os.remove(path)
                ctx.debug("已删除 {}".format(path))
            except OSError as exc:
                ctx.warn("删除 {} 失败：{}".format(path, exc))


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(content)


# --------------------------------------------------------------- 作者信息
def resolve_author(ctx: TaskContext, author_url: str) -> tuple[str, str, str]:
    """返回 ``(author_id, author_ip, author_name)``。作者名解析失败时可人工输入。"""
    html = get_text(ctx.session(), author_url + "/view",
                    cookies=ctx.account.cookie_dict(),
                    referer=author_url + "/view")
    page = etree.HTML(html)
    author_id = extract_blog_id(html, what="作者归档页")

    m = re.search(r"http[s]*://(.*).lofter.com/", author_url)
    if not m:
        raise ConfigError("作者链接格式不对：{}".format(author_url))
    author_ip = m.group(1)

    try:
        author_name = page.xpath("//title//text()")[0]
    except IndexError:
        author_name = ctx.ask("解析作者名时出现异常，请手动输入作者名", default=author_ip)
        if not author_name:
            author_name = author_ip

    try:
        ctx.log("作者名 {}，lofter 三级域名 {}，主页链接 {}".format(author_name, author_ip, author_url))
    except UnicodeEncodeError:
        ctx.log("作者名含异常符号无法显示，lofter 三级域名 {}，主页链接 {}".format(author_ip, author_url))
    return author_id, author_ip, author_name


# --------------------------------------------------------- 阶段A：解析博客页
def parse_blogs_info(ctx: TaskContext, cfg: AuthorImgConfig, blogs_info: list[dict],
                     parsed_blogs_info: list[dict], author_name: str, author_ip: str,
                     file_path: str, paths: dict, last_img_state: dict) -> None:
    """逐篇打开博客页，抠出图片链接写进 ``imgs_info.json``。

    每次取列表第一个元素处理完就删掉，并定期把三个列表刷回磁盘，
    这样中途失败可以接着跑（与原脚本一致）。
    """
    imgs_info = read_json(paths[IMGS_FILE], []) or []
    parsed_num = len(parsed_blogs_info)
    interval = max(1, cfg.file_update_interval)

    def flush() -> None:
        write_json(paths[BLOGS_FILE], blogs_info, indent=0)
        write_json(paths[IMGS_FILE], imgs_info, indent=0)
        write_json(paths[PARSED_FILE], parsed_blogs_info, indent=0)
        ctx.debug("文件刷新")

    for blog_num in range(len(blogs_info)):
        ctx.check_cancel()
        blog_url = blogs_info[0]["blog_url"]
        img_time = blogs_info[0]["time"]
        ctx.log("博客 {} 开始解析".format(blog_url))

        try:
            content = get_text(ctx.session(), blog_url,
                               cookies=ctx.account.cookie_dict(), referer=blog_url)
        except NetworkError as exc:
            ctx.warn("打开博客失败，跳过：{}".format(exc))
            parsed_blogs_info.append(blogs_info.pop(0))
            continue

        blog_tags = re.findall(r'"http[s]{0,1}://.*?.lofter.com/tag/(.*?)"', content)
        from urllib.parse import unquote

        blog_tags = [unquote(t, "utf-8").replace("\xa0", " ") for t in blog_tags]

        if cfg.target_tags and not tag_match(blog_tags, cfg.target_tags, cfg.tags_filter_mode):
            blogs_info.pop(0)
            parsed_num += 1
            ctx.log("该篇博客被过滤掉，剩余 {}".format(len(blogs_info)))
            if blog_num % interval == 0 or not blogs_info:
                flush()
            continue

        # 不同作者主页模板不同，直接用正则抓所有图片链接，再过滤掉头像/推荐位
        imgs_url = RE_IMG_NEW.findall(content)
        if not filter_img_urls(imgs_url, "img"):
            ctx.debug("新格式正则没匹配到，尝试旧格式")
            imgs_url = RE_IMG_OLD.findall(content)
        imgs_url = filter_img_urls(imgs_url, "img")

        # 同一天发多条博客时，序号要接着上一条的往下排
        img_index = 0
        if img_time == last_img_state.get("last_file_time"):
            img_index = last_img_state.get("index", 0)

        added = 0
        for url in imgs_url:
            img_type = detect_img_type(url)
            img_index += 1
            imgs_info.append({
                "img_url": url,
                "pic_name": "{}[{}] {}({}).{}".format(author_name, author_ip, img_time,
                                                       img_index, img_type),
            })
            added += 1
            last_img_state["last_file_time"] = img_time
            last_img_state["index"] = img_index

        # 下一条博客和本条同一天时不能刷新文件，否则 last_img_state 会丢
        same_time_next = False
        try:
            same_time_next = blogs_info[0]["time"] == blogs_info[1]["time"]
        except IndexError:
            same_time_next = False

        parsed_num += 1
        parsed_blogs_info.append(blogs_info.pop(0))
        ctx.log("解析完成，本条 {} 张图，累计 {} 张，已解析 {} 个链接，剩余 {}".format(
            added, len(imgs_info), parsed_num, len(blogs_info)))
        ctx.progress(parsed_num, parsed_num + len(blogs_info), "解析博客页")
        polite_sleep(0, 1, ctx.reporter)

        if (blog_num % interval == 0 and not same_time_next) or not blogs_info:
            flush()
            polite_sleep(0, 1, ctx.reporter)

    _write_text(paths[BLOGS_FILE], FINISHED)


# --------------------------------------------------------- 阶段B：下载图片
def download_img(ctx: TaskContext, imgs_info: list[dict], imgs_info_saved: list[dict],
                 author_name: str, author_ip: str, author_url: str, file_path: str,
                 paths: dict, interval: int) -> None:
    author_dir = os.path.join(file_path, "img",
                              "{}[{}]".format(sanitize_filename(author_name), author_ip))
    os.makedirs(author_dir, exist_ok=True)
    save_num = len(imgs_info_saved)
    total = save_num + len(imgs_info)
    interval = max(1, interval)

    for img_index in range(len(imgs_info)):
        ctx.check_cancel()
        img = imgs_info[0]
        pic_name = sanitize_filename(img["pic_name"])
        ctx.log("获取图片 {} （{}/{}）".format(img["img_url"], save_num + 1, total))
        try:
            content = fetch_bytes(None, img["img_url"], referer=author_url)
        except NetworkError as exc:
            ctx.warn("图片下载失败，跳过：{}".format(exc))
            imgs_info.pop(0)
            continue
        with open(os.path.join(author_dir, pic_name), "wb") as fp:
            fp.write(content)

        save_num += 1
        imgs_info_saved.append(imgs_info.pop(0))
        ctx.progress(save_num, total, "下载图片")

        if img_index % interval == 0 or not imgs_info:
            write_json(paths[IMGS_FILE], imgs_info, indent=0)
            write_json(paths[SAVED_FILE], imgs_info_saved, indent=0)
            polite_sleep(1, 1, ctx.reporter)

    _write_text(paths[IMGS_FILE], FINISHED)


class AuthorImgTask(BaseTask):
    tid = "author_img"
    name = "保存作者主页的图片（PC 端）"
    summary = "翻作者的归档页，把每一篇博客里的图片按「作者[域名] 日期(序号)」存下来"
    config_class = AuthorImgConfig
    order = 20

    def run(self, ctx: TaskContext, cfg: AuthorImgConfig):
        cfg.validate()
        file_path = self.base_dir_of(cfg)
        os.makedirs(file_path, exist_ok=True)

        author_id, author_ip, author_name = resolve_author(ctx, cfg.author_url)

        if cfg.target_tags:
            ctx.log("tag 过滤已打开，只保存含 {} 的博客；没有 tag 的博客{}保留".format(
                cfg.target_tags, "" if cfg.tags_filter_mode == "in" else "不"))
        else:
            ctx.log("tag 过滤未打开，将保存所有图片")

        paths = init_state_files(file_path, ctx)
        query_num = 50

        # ---- 阶段A
        first_line = _first_line_or_empty(paths[BLOGS_FILE])
        if first_line == FINISHED:
            ctx.log("所有博客已解析完毕，跳转至图片下载")
        elif first_line:
            blogs_info = read_json(paths[BLOGS_FILE], []) or []
            parsed_blogs_info = read_json(paths[PARSED_FILE], []) or []
            ctx.log("接上次继续：未解析 {} 条，已解析 {} 条".format(
                len(blogs_info), len(parsed_blogs_info)))
            parse_blogs_info(ctx, cfg, blogs_info, parsed_blogs_info, author_name, author_ip,
                             file_path, paths, {})
        else:
            ctx.stage("阶段A：获取归档页并解析博客页")
            session = ctx.session(host=cfg.author_url.split("//")[1].replace("/", ""),
                                  referer=cfg.author_url + "/view")
            entries = fetch_archive_entries(
                session, cfg.author_url, author_id, query_num=query_num,
                start_time=cfg.start_time, end_time=cfg.end_time,
                entry_pattern=ENTRY_PATTERN_IMG, reporter=ctx.reporter)
            blogs_info = parse_archive_img_entries(
                entries, cfg.author_url, start_time=cfg.start_time,
                end_time=cfg.end_time, reporter=ctx.reporter)
            parsed_blogs_info = read_json(paths[PARSED_FILE], []) or []
            write_json(paths[BLOGS_FILE], blogs_info, indent=0)
            ctx.log("归档页数据保存完毕，开始解析博客页")
            parse_blogs_info(ctx, cfg, blogs_info, parsed_blogs_info, author_name, author_ip,
                             file_path, paths, {})

        # ---- 阶段B
        ctx.stage("阶段B：下载图片")
        if _first_line_or_empty(paths[IMGS_FILE]) == FINISHED:
            ctx.log("该作者主页的所有图片已保存完毕，无需操作")
        else:
            imgs_info = read_json(paths[IMGS_FILE], []) or []
            imgs_info_saved = read_json(paths[SAVED_FILE], []) or []
            if imgs_info:
                download_img(ctx, imgs_info, imgs_info_saved, author_name, author_ip,
                             cfg.author_url, file_path, paths, cfg.file_update_interval)
                ctx.log("所有图片保存完毕")
            else:
                ctx.log("没有需要下载的图片")

        cleanup_state_files(paths, ctx)
        ctx.log("程序运行结束")
