"""l9：保存某个作者主页的所有文章与文本（原 ``l9_author_txt.py``）。

比 l4 多做三件事：

1. 先用**第一篇**博客自动匹配正文模板（7 个候选 + 通用兜底），后续所有博客复用；
2. 可选抓评论；
3. 可选章节合并（把标题里含指定词的文章按发表顺序拼成一本）。

迁移时保留了一个坑位提示：原 ``merge_chapter_al``（章节合并 v2）写文件写在了循环里，
属于废弃分支，这里保留同名函数但修正了缩进，且默认不会被调用。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from collections import Counter
from urllib.parse import unquote

from lxml.html import etree

from .. import templates
from ..archive import ENTRY_PATTERN_TXT, fetch_archive_entries, parse_archive_txt_entries
from ..config import AuthorTxtConfig
from ..errors import ConfigError, NetworkError
from ..net import extract_blog_id, get_text
from ..progress import read_json
from ..utils import (dedup_filename, filter_img_urls, js_unescape_utf8, tag_match,
                     ts_to_datetime_str)
from .base import BaseTask, TaskContext

__all__ = ["AuthorTxtTask"]

COMMENT_DWR = "https://www.lofter.com/dwr/call/plaincall/PostBean.getPostResponses.dwr"
RE_IMG_NEW = re.compile(r'"(http[s]{0,1}://imglf\d{0,1}.lf\d*.[0-9]{0,3}.net.*?)"')
RE_IMG_OLD = re.compile(r'"(http[s]{0,1}://imglf\d.nosdn\d*.[0-9]{0,3}\d.net.*?)"')


# --------------------------------------------------------------- 作者信息
def resolve_author(ctx: TaskContext, author_url: str) -> tuple[str, str, str]:
    html = get_text(ctx.session(), author_url + "/view",
                    cookies=ctx.account.cookie_dict(), referer=author_url + "/view")
    page = etree.HTML(html)
    author_id = extract_blog_id(html, what="作者归档页")

    m = re.search(r"http[s]*://(.*).lofter.com/", author_url)
    if not m:
        raise ConfigError("作者链接格式不对：{}".format(author_url))
    author_ip = m.group(1)

    try:
        author_name = page.xpath("//title//text()")[0].replace("归档 - ", "")
    except IndexError:
        author_name = ctx.ask("解析作者名时出现异常，请手动输入作者名", default=author_ip)
        if not author_name:
            author_name = author_ip
    ctx.log("作者名 {}，lofter 三级域名 {}，主页链接 {}".format(author_name, author_ip, author_url))
    return author_id, author_ip, author_name


# ------------------------------------------------------------------ 抓评论
def fetch_comments(ctx: TaskContext, parse) -> list[str]:
    """抓一篇博客下的所有评论，返回排好序的文本行。"""
    frames = parse.xpath("//iframe[@id='comment_frame']/@src")
    if not frames:
        return []
    m = re.search(r"pid=(\d+)&bid=", frames[0])
    if not m:
        return []

    param0 = m.group(1)
    headers = {
        "Host": "www.lofter.com",
        "Origin": "https://www.lofter.com",
        "Referer": "https:" + frames[0],
        "Accept-Encoding": "gzip, deflate",
    }
    number1, number2 = 50, 0
    all_comm_str = ""
    comm_list: list[str] = []

    while True:
        ctx.check_cancel()
        comm_data = {
            "callCount": "1",
            "scriptSessionId": "${scriptSessionId}187",
            "httpSessionId": "",
            "c0-scriptName": "PostBean",
            "c0-methodName": "getPostResponses",
            "c0-id": "0",
            "c0-param0": "number:{}".format(param0),
            "c0-param1": "number:{}".format(number1),
            "c0-param2": "number:{}".format(number2),
            "batchId": "334950",
        }
        number2 += number1
        try:
            resp = ctx.session().post(COMMENT_DWR, data=comm_data, headers=headers, timeout=30)
            comm_text = resp.content.decode("utf-8")
        except Exception as exc:
            ctx.warn("抓评论失败：{}".format(exc))
            break
        all_comm_str += comm_text
        comm_infos = comm_text.split("anonymousUser")[1:]
        if not comm_infos:
            break

        for comm_info in comm_infos:
            try:
                sid = re.search(r"(s\d+)\.appVersion", comm_info).group(1)
                comm_content = js_unescape_utf8(
                    re.search(sid + r'\.content="(.*?)";', comm_info).group(1))
                publish_time = re.search(sid + r'\.publishTime=(\d+);', comm_info).group(1)
                published = ts_to_datetime_str(publish_time)

                publisher_sid = re.search(sid + r"\.publisherMainBlogInfo=(.*?);", comm_info).group(1)
                publisher_nickname = _nickname(publisher_sid, comm_info, all_comm_str)
                publisher_blogname = _blogname(publisher_sid, comm_info, all_comm_str)

                reply_sid = re.search(sid + r"\.replyBlogInfo=(.*?);", comm_info).group(1)
                if reply_sid == "null":
                    reply_nickname = reply_blogname = ""
                else:
                    reply_nickname = _nickname(reply_sid, comm_info, all_comm_str)
                    reply_blogname = _blogname(reply_sid, comm_info, all_comm_str)

                if reply_nickname:
                    comm_list.append("{} {}[{}] 回复 {}[{}]：{}".format(
                        published, publisher_nickname, publisher_blogname,
                        reply_nickname, reply_blogname, comm_content))
                else:
                    comm_list.append("{}  {}[{}]：{}".format(
                        published, publisher_nickname, publisher_blogname, comm_content))
            except (AttributeError, IndexError):
                continue

    return comm_list[::-1]


def _nickname(sid: str, comm_info: str, all_comm_str: str) -> str:
    match = re.search(sid + r'\.blogNickName="(.*?)";', comm_info)
    if not match:
        match = re.search(sid + r'\.blogNickName="(.*?)";', all_comm_str)
    if not match:
        return ""
    return js_unescape_utf8(match.group(1))


def _blogname(sid: str, comm_info: str, all_comm_str: str) -> str:
    match = re.search(sid + r'\.blogName="(.*?)";', comm_info)
    if not match:
        match = re.search(sid + r'\.blogName="(.*?)";', all_comm_str)
    if not match:
        return ""
    return js_unescape_utf8(match.group(1))


# ------------------------------------------------------------------ 保存
def save_file(ctx: TaskContext, cfg: AuthorTxtConfig, blog_infos: list[dict],
              author_name: str, author_ip: str, article_path: str) -> list[str]:
    """逐篇保存文章/文本，返回保存下来的文件名（按归档顺序）。"""
    if not blog_infos:
        return []

    # 用第一篇匹配模板，之后全部复用
    try:
        first_html = get_text(ctx.session(), blog_infos[0]["url"],
                              cookies=ctx.account.cookie_dict(), referer=blog_infos[0]["url"])
        template_id = templates.matcher(etree.HTML(first_html))
    except NetworkError as exc:
        ctx.warn("读取首篇博客失败：{}".format(exc))
        template_id = templates.TEMPLATE_GENERIC

    ctx.log("文字匹配模板为模板 {}".format(template_id))
    if template_id == templates.TEMPLATE_GENERIC:
        ctx.warn("模板 0 是通用兜底模板，正文之外可能掺入别的内容，也可能有内容缺失。")
        # 原脚本在这里强制 input("ok")；无人值守时只告警不阻塞
        if not ctx.confirm_if_interactive("使用通用模板继续保存？", default=True):
            ctx.log("已退出（未保存）")
            return []

    all_file_name: list[str] = []
    total = len(blog_infos)
    join_word = "\n" * max(0, int(cfg.additional_break))

    for index, blog in enumerate(blog_infos, 1):
        ctx.check_cancel()
        title = blog["title"]
        print_title = blog["print_title"]
        public_time = blog["time"]
        url = blog["url"]
        blog_type = blog["blog_type"]
        ctx.log("准备保存：{}，原文链接：{}".format(print_title, url))

        if blog_type == "article":
            head = "{} by {}[{}]\n发表时间：{}\n原文链接： {}".format(
                title, author_name, author_ip, public_time, url)
        else:
            head = "{}\n原文链接： {}".format(title, url)

        try:
            content = get_text(ctx.session(), url, cookies=ctx.account.cookie_dict(), referer=url)
        except NetworkError as exc:
            ctx.warn("读取博客失败，跳过：{}".format(exc))
            continue

        blog_tags = re.findall(r'"http[s]{0,1}://.*?.lofter.com/tag/(.*?)"', content)
        blog_tags = [unquote(t, "utf-8").replace("\xa0", " ") for t in blog_tags]
        if not tag_match(blog_tags, cfg.target_tags, cfg.tags_filter_mode):
            ctx.log("博客 {} 不包含指定 tag，跳过".format(print_title))
            continue

        parse = etree.HTML(content)
        article_content = templates.get_content(parse, template_id, title, blog_type, join_word)

        comments = fetch_comments(ctx, parse) if cfg.get_comm else []

        illustration = RE_IMG_NEW.findall(content)
        if not filter_img_urls(illustration, blog_type):
            illustration = RE_IMG_OLD.findall(content)
        illustration = filter_img_urls(illustration, blog_type)
        tail = ("博客中包含的图片：\n" + "\n".join(illustration)) if illustration else ""

        article = head + "\n\n\n\n" + article_content + "\n\n\n" + tail
        if comments:
            article += "\n\n\n-----评论-----\n\n" + "\n".join(comments)

        if blog_type == "article":
            file_name = "{} by {}.txt".format(title, author_name)
        else:
            file_name = "{}.txt".format(title)
        from ..utils import sanitize_filename

        file_name = sanitize_filename(file_name, fullwidth_comma=True)
        file_name = dedup_filename(file_name, article, article_path, "txt")

        with open(os.path.join(article_path, file_name), "w", encoding="utf-8") as fp:
            fp.write(article)
        ctx.log("{} 保存完毕".format(file_name))
        all_file_name.append(file_name)
        ctx.progress(index, total, "保存文章/文本")

        time.sleep(0.5)

    return all_file_name


# --------------------------------------------------------------- 章节合并
def merge_chapter(ctx: TaskContext, merge_titles: list[str], auto_detect: bool,
                  file_path: str, additional_index: bool,
                  all_file_names: list[str]) -> None:
    """章节合并 v3：按归档顺序（即发表顺序）拼接同名章节。"""
    if not all_file_names:
        ctx.warn("没有可合并的文件")
        return
    ctx.log("开始章节合并")
    merge_path = os.path.join(file_path, "merge_file")
    origin_path = os.path.join(file_path, "origin_file")
    for path in (merge_path, origin_path):
        os.makedirs(path, exist_ok=True)

    author_name = all_file_names[0].split("by ")[-1].split(".txt")[0]

    if auto_detect:
        ctx.log("已开启自动章节合并，检索需合并的文章")
        counter: Counter = Counter()
        for filename in all_file_names:
            counter[filename.split("(")[0].split("（")[0]] += 1
        for name, count in counter.items():
            if count > 1 and name not in merge_titles:
                merge_titles.append(name)

    ctx.log("以下标题的文章将被合并：{}".format(merge_titles))
    moved: set[str] = set()
    for merge_title in merge_titles:
        chapters = [name for name in all_file_names if merge_title in name]
        if not chapters:
            continue
        moved.update(chapters)

        result = ""
        for index, chapter in enumerate(chapters, 1):
            with open(os.path.join(file_path, chapter), "r", encoding="utf-8") as fp:
                body = fp.read()
            if additional_index:
                body = "第{}章\n".format(index) + body
            result += body + "\n\n"
        with open(os.path.join(merge_path, "{} by {}.txt".format(merge_title, author_name)),
                  "w", encoding="utf-8") as fp:
            fp.write(result)
        ctx.log("{} 共 {} 章，合并完成".format(merge_title, len(chapters)))
        ctx.log("包括文件：{}".format(
            [name.split("by")[0] for name in chapters] if _printable(chapters) else "[文件名含特殊字符，无法输出]"))

    for filename in moved:
        shutil.move(os.path.join(file_path, filename), os.path.join(origin_path, filename))
    ctx.log("章节合并结束，合并后的文件位于 {}，源文件已移动到 {}".format(merge_path, origin_path))


def _printable(names: list[str]) -> bool:
    """文件名里可能有 emoji，打印时会炸（原脚本踩过），这里先探测一下。"""
    import sys

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        for name in names:
            name.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class AuthorTxtTask(BaseTask):
    tid = "author_txt"
    name = "保存作者主页的文章与文本（PC 端）"
    summary = "翻作者归档页，把文章/文本正文抽出来存成 txt，可选抓评论与章节合并"
    config_class = AuthorTxtConfig
    order = 30

    def run(self, ctx: TaskContext, cfg: AuthorTxtConfig):
        cfg.validate()
        base_dir = self.base_dir_of(cfg)
        author_id, author_ip, author_name = resolve_author(ctx, cfg.author_url)

        article_root = os.path.join(base_dir, "article")
        # 与原脚本一致：作者名里的非法字符统一换成下划线（保证老用户的目录名不变）
        article_path = os.path.join(article_root, re.sub(r'[<>:"/\\|?*]', "_", author_name))
        for path in (article_root, article_path):
            os.makedirs(path, exist_ok=True)

        ctx.stage("阶段1：获取归档页")
        session = ctx.session(host=cfg.author_url.split("//")[1].replace("/", ""),
                              referer=cfg.author_url + "/view")
        entries = fetch_archive_entries(
            session, cfg.author_url, author_id, query_num=50,
            start_time=cfg.start_time, end_time=cfg.end_time,
            entry_pattern=ENTRY_PATTERN_TXT, reporter=ctx.reporter)
        blog_infos = parse_archive_txt_entries(
            entries, cfg.author_url, author_name, start_time=cfg.start_time,
            end_time=cfg.end_time, reporter=ctx.reporter)

        if not blog_infos:
            ctx.warn("作者主页中无文本/文字博客，无需爬取")
            return

        ctx.stage("阶段2：抓取并保存正文")
        all_file_name = save_file(ctx, cfg, blog_infos, author_name, author_ip, article_path)
        all_file_name.reverse()  # 归档是从新到旧，反转成发表顺序
        ctx.log("保存完毕，共 {} 篇".format(len(all_file_name)))

        if cfg.chapter_merge_title or cfg.auto_chapter_merge_title:
            ctx.stage("阶段3：章节合并")
            merge_chapter(ctx, list(cfg.chapter_merge_title), bool(cfg.auto_chapter_merge_title),
                          article_path, bool(cfg.additional_chapter_index), all_file_name)
