"""l13：保存我的喜欢 / 我的推荐 / 某个 tag 下的内容（原 ``l13_like_share_tag.py``）。

流程与原脚本完全一致的四阶段 + 断点文件语义：

===== ========================================= ==========================
阶段   做什么                                     结束标志
===== ========================================= ==========================
1      抓归档页原始数据 → 解析成结构化 json        ``format_blogs_info.json``
2      按自动整理选项补 ``key tag``，再按类型分类   ``classified_blogs_info.json``
3      博客类型统计 + tag 频次统计                 无（纯输出）
4      写文章/文本/长文章，下载图片                ``img_save_info.json``（仅图片）
===== ========================================= ==========================

相对原脚本的**行为差异**（都写在对应代码注释里，这里汇总）：

* 进入保存阶段前的 ``input("ok")``、结尾的 ``input("yes/no")`` 改成配置项
  ``pause_before_save`` / ``reset_after_save``，默认都不阻塞；
* 单条博客某字段缺失时跳过并告警，不再让整轮 8000 条一起崩；
* ``eval()`` 解析图片列表改为 ``ast.literal_eval``；
* 所有正则改成 raw string（消掉 3.12 的 21 处 SyntaxWarning）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Optional
from urllib import parse as urlparse

import html2text
from lxml.html import etree

from ..config import LikeShareTagConfig
from ..errors import ConfigError, NetworkError, ParseError
from ..progress import StageFiles
from ..utils import (dedup_filename, is_lofter_img, js_unescape_latin, js_unescape_utf8,
                     literal_list, normalize_tags, pick_best_img_url, sanitize_filename,
                     sanitize_path_part, split_tags, ts_ms_to_date)
from .base import BaseTask, TaskContext

__all__ = ["LikeShareTagTask"]

DWR_LIKE1 = "https://www.lofter.com/dwr/call/plaincall/BlogBean.queryLikePosts.dwr"
DWR_LIKE2 = "http://www.lofter.com/dwr/call/plaincall/PostBean.getFavTrackItem.dwr"
DWR_SHARE = "https://www.lofter.com/dwr/call/plaincall/BlogBean.querySharePosts.dwr"
DWR_TAG = "http://www.lofter.com/dwr/call/plaincall/TagBean.search.dwr"

MODE_URLS = {"share": DWR_SHARE, "like1": DWR_LIKE1, "like2": DWR_LIKE2, "tag": DWR_TAG}
MODE_LABELS = {"like1": "我的喜欢页面", "like2": "我的喜欢页面", "share": "我的推荐页面", "tag": "tag页面"}
TYPE_LABELS = {
    "img": "图片博客", "article": "文章博客（带标题）",
    "text": "文字博客（无标题）", "long article": "长文章", "all": "全部博客",
}

# 正文/字段提取用的正则（全部 raw string）
RE_BLOG_URL = re.compile(r's\d{1,5}.blogPageUrl="(.*?)"')
RE_OP_TIME = re.compile(r"s\d{1,5}.opTime=(.*?);")
RE_HOT = re.compile(r"s\d{1,5}.hot=(.*?);")
RE_NICK = re.compile(r's\d{1,5}.blogNickName="(.*?)"')
RE_BLOG_INFO_REF = re.compile(r"s\d{1,5}.blogInfo=(s\d{1,5})")
RE_AUTHOR_IP = re.compile(r"http[s]{0,1}://(.*?).lofter.com")
RE_PUBLISH_TIME = re.compile(r"s\d{1,5}.publishTime=(.*?);")
RE_TAGS = re.compile(r's\d{1,5}.tag[s]{0,1}="(.*?)";')
RE_TITLE = re.compile(r's\d{1,5}.title="(.*?)"')
RE_PHOTO_LINKS = re.compile(r'originPhotoLinks="(\[.*?\])"')
RE_CONTENT = re.compile(r's\d{1,5}.content="(.*?)";')
RE_ILLUSTRATION_NEW = re.compile(r'"(http[s]{0,1}://imglf\d{0,1}.lf\d*.[0-9]{0,3}.net.*?)\?')
RE_ILLUSTRATION_OLD = re.compile(r'"(http[s]{0,1}://imglf\d{0,1}.nosdn\d*.[0-9]{0,3}.net.*?)\?')
RE_LONG_ARTICLE = re.compile(r's\d{1,5}.compositeContent="(.*?)";s\d{1,5}')
RE_BANNER = re.compile(r's\d{1,5}.banner="(.*?)";')
RE_STRIP_TAGS = re.compile(r"<[^<]+?>")


# =============================================================== 请求参数构造
def make_data(mode: str, url: str, *, session, reporter) -> dict:
    """构造 DWR 请求参数。

    like1/share 需要先去作者主页拿数字 blogId；like2 靠 cookie；tag 要拿时间戳。
    """
    if mode in ("like1", "share", "tag") and not url:
        raise ConfigError("{} 模式必须提供链接".format(mode))

    base_data = {
        "callCount": "1",
        "httpSessionId": "",
        "scriptSessionId": "${scriptSessionId}187",
        "c0-id": "0",
        "batchId": "472351",
    }
    get_num, got_num = 100, 0

    if mode in ("share", "like1"):
        host = re.search(r"https?://(.*?)/", url)
        if not host:
            raise ConfigError("作者主页链接格式不对：{}".format(url))
        from ..net import extract_blog_id, get_text

        # 与原脚本一致：请求作者主页时要带 Host 头
        session.headers["Host"] = host.group(1)
        html = get_text(session, url, referer=url)
        user_id = extract_blog_id(html, what="用户主页")
        parme = {
            "c0-scriptName": "BlogBean",
            "c0-methodName": "queryLikePosts" if mode == "like1" else "querySharePosts",
            "c0-param0": "number:" + str(user_id),
            "c0-param1": "number:" + str(get_num),
            "c0-param2": "number:" + str(got_num),
            "c0-param3": "string:",
        }
    elif mode == "like2":
        parme = {
            "c0-scriptName": "PostBean",
            "c0-methodName": "getFavTrackItem",
            "c0-param0": "number:" + str(get_num),
            "c0-param1": "number:" + str(got_num),
        }
    else:  # tag
        m = re.search(r"http[s]{0,1}://www.lofter.com/tag/(.*?)/(.*)", url)
        if not m:
            raise ConfigError("tag 链接格式不对，应该是 https://www.lofter.com/tag/<tag>/<new|total|month|week|date>")
        list_type = m.group(2) or "new"
        parme = {
            "c0-scriptName": "TagBean",
            "c0-methodName": "search",
            "c0-param0": "string:" + m.group(1),
            "c0-param1": "number:0",
            "c0-param2": "string:",
            "c0-param3": "string:" + list_type,
            "c0-param4": "boolean:false",
            "c0-param5": "number:0",
            "c0-param6": "number:" + str(get_num),
            "c0-param7": "number:" + str(got_num),
            "c0-param8": "number:" + str(int(time.time() * 1000)),
            "batchId": "870178",
        }
    return {**base_data, **parme}


def update_data(mode: str, data: dict, get_num: int, got_num: int,
                last_timestamp: str = "0") -> dict:
    """翻页时要更新参数，否则永远拿到第一页。"""
    if mode in ("share", "like1"):
        data["c0-param1"] = "number:" + str(get_num)
        data["c0-param2"] = "number:" + str(got_num)
    elif mode == "like2":
        data["c0-param0"] = "number:" + str(get_num)
        data["c0-param1"] = "number:" + str(got_num)
    elif mode == "tag":
        data["c0-param6"] = "number:" + str(get_num)
        data["c0-param7"] = "number:" + str(got_num)
        data["c0-param8"] = "number:" + str(last_timestamp)
    return data


# ================================================================= 阶段1：抓取
def fetch_raw(ctx: TaskContext, cfg: LikeShareTagConfig, file_path: str) -> str:
    """翻页抓原始 DWR 响应，落盘到 ``blogs_info``，返回文件内容。

    ``activityTags`` 一般出现在每条记录的前两个属性里，从它切分能保证记录完整。
    """
    session = ctx.login_session()
    data = make_data(cfg.mode, cfg.url, session=session, reporter=ctx.reporter)
    requests_url = MODE_URLS[cfg.mode]
    get_num = 100
    got_num = 0
    real_got_num = 0
    fav_info: list[str] = []
    start_ts = time.mktime(time.strptime(cfg.start_time, "%Y-%m-%d")) if cfg.start_time else 0

    while True:
        ctx.check_cancel()
        ctx.log("正在获取 {}-{}".format(got_num, got_num + get_num))
        content = _post_dwr(session, requests_url, data)
        new_info = content.split("activityTags")[1:]
        fav_info += new_info
        got_num += get_num
        real_got_num += len(new_info)
        ctx.progress(got_num, 0, "已获取 {} 条".format(real_got_num))
        ctx.debug("实际返回条数 {}".format(len(new_info)))

        if not new_info:
            ctx.log("已获取到最后一页，{}信息获取完成".format(MODE_LABELS.get(cfg.mode, "")))
            break

        if cfg.mode == "like2":
            last_timestamp, last_optime = 0, "/"
            try:
                last_timestamp = int(RE_OP_TIME.search(new_info[-1]).group(1)) / 1000
                last_optime = time.strftime("%Y-%m-%d", time.localtime(last_timestamp))
            except (AttributeError, ValueError):
                pass
            ctx.debug("最后一条的点赞时间为 {}".format(last_optime))
            if cfg.start_time and last_timestamp < start_ts:
                ctx.log("已获取到指定时间内所有博客，信息获取完成")
                break
        elif cfg.mode == "tag":
            try:
                ctx.debug("最后一条热度为 {}".format(int(RE_HOT.search(new_info[-1]).group(1))))
            except (AttributeError, ValueError):
                pass
            m = re.search(r"s\d{1,5}.publishTime=(.*?);", new_info[-1])
            if m:
                data = update_data(cfg.mode, data, get_num, got_num, m.group(1))
        else:
            data = update_data(cfg.mode, data, get_num, got_num)

    if not fav_info:
        raise ParseError("归档页获取异常：请检查网络、模式与链接是否匹配；"
                         "like2 请检查登录信息是否有效。")

    with open(os.path.join(file_path, StageFiles.RAW), "w", encoding="utf-8") as fp:
        for info in fav_info:
            fp.write(info.replace("\n", ""))
            fp.write("\n\nsplit_line\n\n")
    ctx.log("总请求条数：{}  实际返回条数：{}".format(got_num, real_got_num))
    return open(os.path.join(file_path, StageFiles.RAW), encoding="utf-8").read()


def _post_dwr(session, url: str, data: dict) -> str:
    from ..net import post_dwr

    return post_dwr(session, url, data)


# ============================================================== 阶段1：解析
def format_entries(ctx: TaskContext, cfg: LikeShareTagConfig, favs_info: list[str],
                   fav_str: str, file_path: str) -> None:
    """把原始记录解析成结构化 json，写 ``format_blogs_info.json``。"""
    format_info: list[dict] = []
    start_ts = time.mktime(time.strptime(cfg.start_time, "%Y-%m-%d")) if cfg.start_time else 0

    for index, fav_info in enumerate(favs_info):
        ctx.check_cancel()
        blog = {}

        m = RE_BLOG_URL.search(fav_info)
        if not m:
            ctx.warn("第 {} 条博客信息丢失，跳过".format(index + 1))
            continue
        blog["url"] = m.group(1)

        # 点赞时间：like2 早于设定时间就整段结束
        try:
            fav_timestamp = RE_OP_TIME.search(fav_info).group(1)
            if cfg.mode == "like2" and cfg.start_time and int(fav_timestamp) / 1000 < start_ts:
                ctx.log("已将指定时间内的博客解析结束")
                break
            blog["fav time"] = ts_ms_to_date(fav_timestamp)
        except (AttributeError, ValueError):
            blog["fav time"] = ""

        # 热度：tag 模式可以设阈值过滤
        try:
            blog_hot = int(RE_HOT.search(fav_info).group(1))
            if cfg.mode == "tag" and blog_hot < cfg.min_hot:
                continue
        except (AttributeError, ValueError):
            pass

        # 作者名：本条没有就往前找同作者的记录
        nick = RE_NICK.search(fav_info)
        if nick:
            author_name = js_unescape_latin(nick.group(1))
        else:
            try:
                info_id = RE_BLOG_INFO_REF.search(fav_info).group(1)
                refs = re.findall(info_id + r'.blogNickName="(.*?)"',
                                  fav_str.split('blogPageUrl="' + blog["url"] + '"')[0])
                author_name = js_unescape_latin(refs[-1])
            except (AttributeError, IndexError):
                ctx.warn("第 {} 条博客解析不出作者名，跳过".format(index + 1))
                continue
        blog["author name"] = author_name
        blog["author name in filename"] = sanitize_filename(author_name)

        m = RE_AUTHOR_IP.search(blog["url"])
        blog["author ip"] = m.group(1) if m else ""

        try:
            blog["public time"] = ts_ms_to_date(RE_PUBLISH_TIME.search(fav_info).group(1))
        except (AttributeError, ValueError):
            blog["public time"] = ""

        try:
            tags = js_unescape_utf8(RE_TAGS.search(fav_info).group(1)).strip()
            blog["tags"] = normalize_tags(split_tags(tags))
        except (AttributeError, ValueError):
            blog["tags"] = []

        try:
            title = js_unescape_latin(RE_TITLE.search(fav_info).group(1))
        except (AttributeError, ValueError):
            title = ""
        blog["title"] = title
        blog["title in filename"] = sanitize_filename(title)

        # 图片链接
        img_urls: list[str] = []
        m = RE_PHOTO_LINKS.search(fav_info)
        if m:
            for url_info in literal_list(m.group(1)):
                if isinstance(url_info, dict):
                    img_urls.append(pick_best_img_url(url_info))
        blog["img urls"] = [u for u in img_urls if u]

        # 正文
        try:
            tmp_content = RE_CONTENT.search(fav_info).group(1)
        except (AttributeError, ValueError):
            tmp_content = ""
        parse = etree.HTML(tmp_content) if tmp_content else None
        blog["content"] = html2text.html2text(js_unescape_latin(tmp_content))

        # 正文里插的图
        illustration: list[str] = []
        if tmp_content and parse is not None:
            img_src = parse.xpath("//img/@src")
            joined = "\n".join(img_src)
            illustration = RE_ILLUSTRATION_NEW.findall(joined) or RE_ILLUSTRATION_OLD.findall(joined)
        blog["illustration"] = illustration

        # 外链
        if tmp_content and parse is not None:
            link_a = parse.xpath("//a/@href")
            blog["external link"] = [x.replace("\\", "").replace('"', "") for x in link_a]
        else:
            blog["external link"] = []

        # 长文章
        l_content, l_cover, l_url, l_img = "", "", [], []
        m = RE_LONG_ARTICLE.search(fav_info)
        if m:
            try:
                long_html = m.group(1)
                l_parse = etree.HTML(long_html)
                banner = RE_BANNER.search(fav_info)
                l_cover = banner.group(1) if banner else ""
                l_url = [x.replace("\\", "").replace('"', "") for x in l_parse.xpath("//a//@href")]
                l_img = [x.replace("\\", "").replace('"', "") for x in l_parse.xpath("//img/@src")]
                l_content = js_unescape_latin(
                    RE_STRIP_TAGS.sub("", long_html).replace("&nbsp;", " ").strip())
            except Exception as exc:  # 长文章被屏蔽时结构不完整
                ctx.debug("长文章 {} 正文不可用：{}".format(blog["url"], exc))
        blog["long article content"] = l_content
        blog["long article url"] = l_url
        blog["long article img"] = l_img
        blog["long article cover"] = l_cover

        format_info.append(blog)

        if cfg.print_level:
            ctx.debug("解析完成：{}".format(blog))
        elif index % 100 == 0 or len(format_info) == len(favs_info):
            ctx.log("解析进度 {}/{}   {}".format(len(format_info), len(favs_info), blog["url"]))
        ctx.progress(len(format_info), len(favs_info), "解析中")

    StageFiles(file_path).write_json(StageFiles.FORMATTED, format_info)
    ctx.log("解析完成，共 {} 条".format(len(format_info)))


# ============================================================== 阶段2：分类
def update_key_tag(blogs_info: list[dict], classify_by_tag: bool, prior_tags: list,
                   agg_non_prior_tag: bool) -> list[dict]:
    """补 ``key tag`` 字段，决定每条博客最终落到哪个目录。"""
    for blog in blogs_info:
        blog["key tag"] = ""
    if not classify_by_tag:
        return blogs_info

    lower_prior = [t.lower() for t in prior_tags]
    for blog in blogs_info:
        if blog["tags"]:
            for original, lowered in zip(prior_tags, lower_prior):
                if lowered in blog["tags"]:
                    blog["key tag"] = original
                    break
            if blog["key tag"] == "":
                if agg_non_prior_tag and prior_tags:
                    blog["key tag"] = "other"
                else:
                    blog["key tag"] = blog["tags"][0]
        else:
            blog["key tag"] = "no tag"
    return blogs_info


def classify(blogs_info: list[dict]) -> dict:
    """按 图片 → 长文章 → 文章 → 文本 的优先级分类（顺序不能换）。"""
    out = {"img": [], "article": [], "long article": [], "text": []}
    for blog in blogs_info:
        if blog["img urls"]:
            out["img"].append(blog)
        elif blog["long article content"]:
            out["long article"].append(blog)
        elif blog["title"]:
            out["article"].append(blog)
        else:
            out["text"].append(blog)
    return out


def count_type(classified: dict) -> dict:
    return {key: len(value) for key, value in classified.items()}


def count_tag(blogs_info: list[dict]) -> dict:
    """tag 频次统计，按出现次数降序。"""
    counter: dict[str, int] = {}
    for blog in blogs_info:
        for tag in blog["tags"]:
            counter[tag] = counter.get(tag, 0) + 1
    return dict(sorted(counter.items(), key=lambda item: item[1], reverse=True))


def get_tail(blog_info: dict) -> str:
    tail = ""
    if blog_info["external link"]:
        tail += "文章中包含的外部连接"
        for link in blog_info["external link"]:
            tail += "\n" + link
    if blog_info["illustration"]:
        tail += "\n\n文章中包含的图片连接："
        for link in blog_info["illustration"]:
            tail += "\n" + link
    return tail


def tag_path(key_tag: str) -> str:
    return sanitize_path_part(key_tag)


# ============================================================== 阶段4：保存
def article_folder(file_path: str, blog: dict, cfg: LikeShareTagConfig, kind: str) -> str:
    """算出这条博客该存到哪个目录（自动整理逻辑集中在这里，原来重复了 2 份）。"""
    key_tag = tag_path(blog["key tag"])
    if not cfg.classify_by_tag:
        return os.path.join(file_path, kind)
    if not cfg.prior_tags:
        return os.path.join(file_path, kind, key_tag)
    if blog["key tag"] in cfg.prior_tags:
        return os.path.join(file_path, kind, "prior", key_tag)
    if cfg.agg_non_prior_tag:
        return os.path.join(file_path, kind, "other")
    return os.path.join(file_path, kind, "other", key_tag)


def save_article(ctx: TaskContext, cfg: LikeShareTagConfig, articles: list[dict],
                 file_path: str) -> None:
    if cfg.classify_by_tag and cfg.prior_tags:
        for name in ("prior", "other"):
            ctx.makedirs("article", name)
    total = len(articles)
    for count, blog in enumerate(articles, 1):
        ctx.check_cancel()
        tags_text = ", ".join(blog["tags"]) or "无"
        head = "{} by {}[{}]\n发表时间：{}\n原文连接：{}\ntags：{}".format(
            blog["title"], blog["author name"], blog["author ip"],
            blog["public time"], blog["url"], tags_text)
        content = head + "\n\n\n" + blog["content"] + "\n\n\n" + get_tail(blog)
        filename = "{} by {}.txt".format(blog["title in filename"], blog["author name in filename"])

        folder = article_folder(file_path, blog, cfg, "article")
        _ensure(folder)
        _write_text(os.path.join(folder, filename), content)

        if cfg.save_img_in_text and blog["illustration"]:
            for img_url in blog["illustration"]:
                ctx.check_cancel()
                try:
                    img = _download(ctx, img_url, blog["url"])
                except NetworkError as exc:
                    ctx.warn("文章插图保存失败（可手动保存）：{}".format(exc))
                    continue
                img_name = "{} by {}.jpg".format(blog["title in filename"],
                                                 blog["author name in filename"])
                img_name = dedup_filename(img_name, img, folder, "jpg")
                _write_bytes(os.path.join(folder, img_name), img)

        _tick(ctx, count, total, "保存文章", cfg.print_level, blog["url"])
    ctx.log("文章保存完成，共 {} 篇".format(total))


def save_text(ctx: TaskContext, cfg: LikeShareTagConfig, texts: list[dict],
              file_path: str) -> None:
    folder = os.path.join(file_path, "text")
    _ensure(folder)
    total = len(texts)
    for count, blog in enumerate(texts, 1):
        ctx.check_cancel()
        tags_text = ", ".join(blog["tags"]) or "无"
        head = "{}[{}]\n发表时间：{}\n原文连接：{}\ntags：{}".format(
            blog["author name"], blog["author ip"], blog["public time"], blog["url"], tags_text)
        content = head + "\n\n\n" + blog["content"] + "\n\n\n" + get_tail(blog)

        first_tag = blog["tags"][0] if blog["tags"] else "无tag"
        filename = "{}-{}-{}.txt".format(blog["author name in filename"], first_tag,
                                         blog["public time"])
        filename = dedup_filename(filename, content, folder, "txt")
        _write_text(os.path.join(folder, filename), content)

        if cfg.save_img_in_text and blog["illustration"]:
            for img_url in blog["illustration"]:
                ctx.check_cancel()
                try:
                    img = _download(ctx, img_url, blog["url"])
                except NetworkError as exc:
                    ctx.warn("文本插图保存失败（可手动保存）：{}".format(exc))
                    continue
                img_name = "{}-{}-{}.jpg".format(blog["author name in filename"], first_tag,
                                                 blog["public time"])
                img_name = dedup_filename(img_name, img, folder, "jpg")
                _write_bytes(os.path.join(folder, img_name), img)

        _tick(ctx, count, total, "保存文本", cfg.print_level, blog["url"], every=10)
    ctx.log("文本保存完成，共 {} 篇".format(total))


def save_long_article(ctx: TaskContext, cfg: LikeShareTagConfig, articles: list[dict],
                      file_path: str) -> None:
    folder = os.path.join(file_path, "long article")
    _ensure(folder)
    total = len(articles)
    for count, blog in enumerate(articles, 1):
        ctx.check_cancel()
        tags_text = ", ".join(blog["tags"]) or "无"
        head = "{} by {}[{}]\n发表时间：{}\n原文连接：{}\ntags：{}".format(
            blog["title"], blog["author name"], blog["author ip"],
            blog["public time"], blog["url"], tags_text)
        tail = ""
        if blog["long article url"]:
            tail += "文章中包含的外部连接"
            for link in blog["long article url"]:
                tail += "\n" + link
        if blog["long article img"]:
            tail += "\n\n文章中包含的图片连接："
            for link in blog["long article img"]:
                tail += "\n" + link
        content = head + "\n\n\n" + blog["long article content"] + "\n\n\n" + tail
        filename = "{} by {}.txt".format(blog["title in filename"], blog["author name in filename"])
        _write_text(os.path.join(folder, filename), content)

        if cfg.save_img_in_text and blog["long article img"]:
            for img_url in blog["long article img"]:
                ctx.check_cancel()
                if not is_lofter_img(img_url):
                    ctx.warn("图片 {} 不是 lofter 站内图，可能会保存失败".format(img_url))
                try:
                    img = _download(ctx, img_url, blog["url"])
                except NetworkError as exc:
                    ctx.warn("长文章插图保存失败（可手动保存）：{}".format(exc))
                    continue
                img_name = "{} by {}.jpg".format(blog["title in filename"],
                                                 blog["author name in filename"])
                img_name = dedup_filename(img_name, img, folder, "jpg")
                _write_bytes(os.path.join(folder, img_name), img)

        _tick(ctx, count, total, "保存长文章", cfg.print_level, blog["url"], every=5, first=True)
    ctx.log("长文章保存完成，共 {} 篇".format(total))


def save_img(ctx: TaskContext, cfg: LikeShareTagConfig, imgs: list[dict], file_path: str,
             img_save_info: dict) -> None:
    """下载图片博客。**支持断点**：进度写在 ``img_save_info.json`` 的「已保存」。"""
    _ensure(os.path.join(file_path, "img"))
    if cfg.classify_by_tag and cfg.prior_tags:
        for name in ("prior", "other"):
            _ensure(os.path.join(file_path, "img", name))

    total = len(imgs)
    saved_num = img_save_info.get("已保存", 0)
    for count, blog in enumerate(imgs):
        if count < saved_num:
            continue
        ctx.check_cancel()
        ctx.log("正在保存：博客序号 {} {}（{} 张图）".format(
            count + 1, blog["url"], len(blog["img urls"])))
        for img_url in blog["img urls"]:
            ctx.check_cancel()
            img_type = "gif" if "gif" in img_url else ("png" if "png" in img_url else "jpg")
            if not is_lofter_img(img_url):
                ctx.warn("图片 {} 不是 lofter 站内图".format(img_url))
            referer = blog["url"].split("post")[0] if is_lofter_img(img_url) else None
            try:
                img = _download(ctx, img_url, referer)
            except NetworkError as exc:
                ctx.warn("保存失败，请尝试手动保存：{}".format(exc))
                continue
            filename = "{}[{}] {}.".format(blog["author name in filename"],
                                           blog["author ip"], blog["public time"]) + img_type
            folder = article_folder(file_path, blog, cfg, "img")
            _ensure(folder)
            filename = dedup_filename(filename, img, folder, img_type)
            _write_bytes(os.path.join(folder, filename), img)

        saved_num += 1
        ctx.progress(saved_num, total, "已保存 {} / {} 条图片博客".format(saved_num, total))
        # 每 7 条刷新一次进度文件（与原脚本一致）
        if saved_num % 7 == 0 or saved_num == total:
            img_save_info["已保存"] = saved_num
            StageFiles(file_path).write_json(StageFiles.IMG_PROGRESS, img_save_info)
    ctx.log("所有图片保存完成")


# ---------------------------------------------------------------- 小工具
def _ensure(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8", errors="ignore") as fp:
        fp.write(content)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fp:
        fp.write(data)


def _download(ctx: TaskContext, img_url: str, referer: Optional[str]) -> bytes:
    from ..net import fetch_bytes

    return fetch_bytes(None, img_url, referer=referer,
                       cookies=ctx.account.cookie_dict())


def _tick(ctx: TaskContext, count: int, total: int, label: str, print_level: int,
          url: str = "", every: int = 20, first: bool = False) -> None:
    if print_level:
        ctx.debug("{} {}/{} {}".format(label, count, total, url))
    elif count % every == 0 or count == total or (first and count == 1):
        ctx.log("{}进度 {}/{}".format(label, count, total))
    ctx.progress(count, total, label)


# ==================================================================== 主流程
class LikeShareTagTask(BaseTask):
    tid = "like_share_tag"
    name = "保存喜欢 / 推荐 / tag（PC 端）"
    summary = "抓「我的喜欢」「我的推荐」或任意 tag 下的内容，按文章/文本/长文章/图片分类落盘"
    config_class = LikeShareTagConfig
    order = 10

    def base_dir_of(self, cfg: LikeShareTagConfig) -> str:
        """l13 把不同模式的进度放在 base_dir 的子目录里，避免互相污染。"""
        base = super().base_dir_of(cfg)
        if cfg.mode == "tag":
            tag_url = cfg.url
            if tag_url.split("/")[-1] not in ("new", "total", "month", "week", "date"):
                tag_url += "/new"
            m = re.search(r"http[s]{0,1}://www.lofter.com/tag/(.*?)/.*", tag_url)
            if not m:
                raise ConfigError("tag 链接格式不对：{}".format(cfg.url))
            tag = sanitize_path_part(urlparse.unquote(m.group(1)))
            return os.path.join(base, "tag_file", tag)
        return os.path.join(base, "{}_file".format(cfg.mode))

    def run(self, ctx: TaskContext, cfg: LikeShareTagConfig):
        cfg.validate()
        file_path = self.base_dir_of(cfg)
        _ensure(file_path)
        stages = StageFiles(file_path)

        # ---- 进度一致性检查（与原脚本逐条对应）
        if not stages.exists(StageFiles.CLASSIFIED) or not stages.exists(StageFiles.FORMATTED):
            if stages.exists(StageFiles.IMG_PROGRESS):
                ctx.warn("找到阶段4进度文件，但阶段1/2文件未找到，删除阶段4进度")
                stages.remove(StageFiles.IMG_PROGRESS)
        if stages.exists(StageFiles.CLASSIFIED) and not stages.exists(StageFiles.FORMATTED):
            ctx.warn("找到阶段2结束文件，但阶段1结束文件未找到，重置进度到阶段1")
            stages.remove(StageFiles.CLASSIFIED)

        if cfg.save_img and stages.exists(StageFiles.IMG_PROGRESS):
            info = stages.read_json(StageFiles.IMG_PROGRESS, {}) or {}
            if info.get("自动整理设置") != cfg.auto_sort_setting:
                stages.remove(StageFiles.CLASSIFIED)
                stages.remove(StageFiles.IMG_PROGRESS)
                ctx.warn("自动整理设置发生更改，进度重置到阶段2，并删除上次保存的图片")
                shutil.rmtree(os.path.join(file_path, "img"), ignore_errors=True)

        # ---- 阶段1
        ctx.stage("阶段1：{}页面信息获取与整理".format(MODE_LABELS.get(cfg.mode, "")))
        if not stages.exists(StageFiles.FORMATTED):
            t0 = time.time()
            fetch_raw(ctx, cfg, file_path)
            raw = open(os.path.join(file_path, StageFiles.RAW), encoding="utf-8").read()
            fav_list = raw.split("\n\nsplit_line\n\n")[0:-1]
            ctx.log("开始解析")
            format_entries(ctx, cfg, fav_list, raw, file_path)
            ctx.log("阶段1耗时 {:.2f} 分钟".format((time.time() - t0) / 60))
        else:
            ctx.log("阶段1在之前的运行中已完成")

        blogs_info = stages.read_json(StageFiles.FORMATTED, [])
        if not blogs_info:
            raise ParseError("阶段1 结果为空，无法继续。删掉 {} 重新运行试试。".format(
                os.path.join(file_path, StageFiles.FORMATTED)))

        # ---- 阶段2
        ctx.stage("阶段2：博客分类")
        if not stages.exists(StageFiles.CLASSIFIED):
            blogs_info = update_key_tag(blogs_info, cfg.classify_by_tag, cfg.prior_tags,
                                       cfg.agg_non_prior_tag)
            classified = classify(blogs_info)
            stages.write_json(StageFiles.CLASSIFIED, classified)
        else:
            ctx.log("阶段2在之前的运行中已完成")
        classified = stages.read_json(StageFiles.CLASSIFIED, {})
        if not classified:
            raise ParseError("阶段2 结果为空，请删掉 {} 重新运行".format(
                os.path.join(file_path, StageFiles.CLASSIFIED)))

        # ---- 阶段3：统计
        ctx.stage("阶段3：博客类型与 tag 统计")
        type_count = count_type(classified)
        for key, value in type_count.items():
            ctx.log("{} {} 篇".format(TYPE_LABELS[key], value))
        ctx.log("共计 {} 篇".format(len(blogs_info)))

        tag_count = {key: count_tag(classified[key]) for key in classified}
        tag_count["all"] = count_tag(blogs_info)
        for key in tag_count:
            filtered = {t: c for t, c in tag_count[key].items() if c > cfg.tag_filt_num}
            ctx.log("{}共计 tag {} 个，出现次数超过 {} 的 tag {} 个".format(
                TYPE_LABELS[key], len(tag_count[key]), cfg.tag_filt_num, len(filtered)))
            ctx.log("各 tag 出现次数统计：{}".format(filtered))

        prior_tags_file = os.path.join(super().base_dir_of(cfg), "prior_tags.txt")
        if not os.path.exists(prior_tags_file):
            _ensure(os.path.dirname(prior_tags_file))
            _write_text(prior_tags_file, "\n".join(tag_count["all"].keys()))
            ctx.log("已生成 {}，可用来挑选优先 tag".format(prior_tags_file))

        ctx.log("自动整理选项：按 tag 整理={} / 优先 tag={} / 非优先聚合={}".format(
            "启动" if cfg.classify_by_tag else "未启动",
            cfg.prior_tags or "未设置",
            "启动" if cfg.agg_non_prior_tag else "未启动"))

        # 原来这里强制 input("ok")，现在改成可选闸门
        if cfg.pause_before_save and ctx.interactive:
            if not ctx.confirm("现在可以改自动整理选项（改完删掉 classified_blogs_info.json 重跑阶段2）。"
                               "继续进入保存阶段？", default=True):
                ctx.warn("已停在阶段3，未进入保存阶段")
                return

        # ---- 阶段4
        ctx.stage("阶段4：保存文件")
        if cfg.save_article:
            _reset_dir(os.path.join(file_path, "article"), "文章")
            if classified["article"]:
                save_article(ctx, cfg, classified["article"], file_path)
            else:
                ctx.log("无文章")
        if cfg.save_text:
            _reset_dir(os.path.join(file_path, "text"), "文本")
            if classified["text"]:
                save_text(ctx, cfg, classified["text"], file_path)
            else:
                ctx.log("无文本")
        if cfg.save_long_article:
            _reset_dir(os.path.join(file_path, "long article"), "长文章")
            if classified["long article"]:
                save_long_article(ctx, cfg, classified["long article"], file_path)
            else:
                ctx.log("无长文章")

        if cfg.save_img:
            t0 = time.time()
            progress_file = os.path.join(file_path, StageFiles.IMG_PROGRESS)
            if not os.path.exists(progress_file):
                img_save_info = {
                    "图片博客总数": len(classified["img"]),
                    "已保存": 0,
                    "自动整理设置": cfg.auto_sort_setting,
                }
                stages.write_json(StageFiles.IMG_PROGRESS, img_save_info)
                ctx.log("新建保存进度 {}".format(img_save_info))
            else:
                img_save_info = stages.read_json(StageFiles.IMG_PROGRESS, {}) or {}
                ctx.log("读取到上次的进度 {}".format(img_save_info))
            if classified["img"]:
                save_img(ctx, cfg, classified["img"], file_path, img_save_info)
            else:
                ctx.log("无图片博客")
            ctx.log("本次保存耗时 {:.2f} 分钟".format((time.time() - t0) / 60))

        # 原脚本结尾要手动输 yes/no，这里改成配置项
        if cfg.reset_after_save:
            removed = stages.reset_all_files()
            ctx.log("已重置进度文件：{}".format(removed or "无"))
        ctx.log("全部完成")


def _reset_dir(path: str, label: str) -> None:
    """文章/文本/长文章每次重跑都会清空重建（与原脚本一致）。"""
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
