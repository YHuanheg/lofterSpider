"""归档页（ArchiveBean）抓取与解析。

l4（作者图片）和 l9（作者文本）原本各写了一份几乎一样的 ``parse_archive_page``，
差别只在两处：

* 单条博客信息的正则结尾不同——l4 用 ``.*noticeLinkTitle``，l9 用 ``.*\\n``；
* 解析出的字段不同——l4 要缩略图，l9 要标题/正文类型。

这里统一成「抓取原始条目」+「按用途解析」两步，正则差异用 :data:`ENTRY_PATTERN_IMG`
/ :data:`ENTRY_PATTERN_TXT` 表达，保证两边行为都不变。
"""

from __future__ import annotations

import re
from typing import Iterable

from .errors import ParseError
from .net import get_headers, post_dwr
from .utils import js_unescape_latin, now_ts_ms, ts_ms_to_date

__all__ = [
    "ARCHIVE_URL_TMPL",
    "ENTRY_PATTERN_IMG",
    "ENTRY_PATTERN_TXT",
    "make_archive_data",
    "make_archive_head",
    "fetch_archive_entries",
    "parse_archive_img_entries",
    "parse_archive_txt_entries",
]

ARCHIVE_URL_TMPL = "{author_url}dwr/call/plaincall/ArchiveBean.getArchivePostByTime.dwr"

# l4 用的条目正则
ENTRY_PATTERN_IMG = re.compile(r"s[\d]*.blogId.*\n.*noticeLinkTitle")
# l9 用的条目正则
ENTRY_PATTERN_TXT = re.compile(r"s[\d]*.blogId.*\n.*\n")

_TIME_RE = re.compile(r"s[\d]*.time=(\d*);")
_LAST_TIME_RE = re.compile(r"s%d\.time=(.*);s.*type")
_IMG_URL_RE = re.compile(r'[\d]*.imgurl="(.*?)"')
_PERMALINK_STRICT_RE = re.compile(r's[\d]*.permalink="(.*?)";')
_PERMALINK_LOOSE_RE = re.compile(r's[\d]*.permalink="(.*)"')
_TITLE_RE = re.compile(r'[\d]*\.title="(.*?)";')
_CONTENT_RE = re.compile(r'[\d]*\.content="(.*?)";')


def make_archive_data(author_id: str, query_num: int = 50) -> dict:
    """构造 ArchiveBean 的 POST 数据。``c0-param2`` 是「从这个时间戳往前翻」。"""
    return {
        "callCount": "1",
        "scriptSessionId": "${scriptSessionId}187",
        "httpSessionId": "",
        "c0-scriptName": "ArchiveBean",
        "c0-methodName": "getArchivePostByTime",
        "c0-id": "0",
        "c0-param0": "boolean:false",
        "c0-param1": "number:" + str(author_id),
        "c0-param2": "number:" + str(now_ts_ms()),
        "c0-param3": "number:" + str(query_num),
        "c0-param4": "boolean:false",
        "batchId": "918906",
    }


def make_archive_head(author_url: str) -> dict:
    """归档接口需要的请求头。"""
    host = author_url.split("//")[1].replace("/", "")
    return get_headers({
        "Host": host,
        "Origin": author_url,
        "Referer": author_url + "/view",
    })


def fetch_archive_entries(session, author_url: str, author_id: str, *,
                          query_num: int = 50,
                          start_time: str = "",
                          end_time: str = "",
                          entry_pattern: re.Pattern = ENTRY_PATTERN_IMG,
                          require_exact_page: bool = True,
                          reporter=None) -> list[str]:
    """翻完归档页，返回**原始条目字符串**列表（未解析字段）。

    :param require_exact_page: 返回条数不等于 ``query_num`` 时认为已到末页。
    :param start_time: ``"YYYY-MM-DD"``，翻到比它还早的页就停。
    """
    from .utils import parse_date_to_ts  # 局部导入避免循环

    archive_url = ARCHIVE_URL_TMPL.format(author_url=author_url)
    data = make_archive_data(author_id, query_num)
    headers = make_archive_head(author_url)
    # 固定一个 session，让 NTESwebSI 之类的 cookie 能延续
    session.headers.update(headers)

    start_ts = parse_date_to_ts(start_time) if start_time else 0.0
    entries: list[str] = []

    while True:
        if reporter:
            reporter.check_cancel()
            reporter.debug("获取归档页，当前时间戳参数 {}".format(data["c0-param2"]))
        page_data = post_dwr(session, archive_url, data)
        new_entries = entry_pattern.findall(page_data)
        entries += new_entries

        if require_exact_page and len(new_entries) != query_num:
            break

        if start_ts:
            param = float(data["c0-param2"].split(":")[1]) / 1000
            if param < start_ts:
                break

        match = _LAST_TIME_RE.search(page_data)
        if not match:
            break
        data["c0-param2"] = "number:" + str(match.group(1))

        from .net import polite_sleep
        polite_sleep(1, 2, reporter)

    return entries


def _entry_timestamp(entry: str) -> str:
    match = _TIME_RE.search(entry)
    if not match:
        raise ParseError("归档条目里没有 time 字段：{}".format(entry[:80]))
    return match.group(1)


def parse_archive_img_entries(entries: Iterable[str], author_url: str, *,
                              start_time: str = "",
                              end_time: str = "",
                              reporter=None) -> list[dict]:
    """l4 用途：只保留**带图**的博客，产出 ``{img_url, blog_url, time}``。"""
    from .utils import parse_date_to_ts

    start_ts = parse_date_to_ts(start_time) if start_time else 0.0
    end_ts = parse_date_to_ts(end_time) if end_time else 0.0
    out: list[dict] = []
    blog_num = 0

    for entry in entries:
        try:
            timestamp = _entry_timestamp(entry)
        except ParseError:
            continue
        ts_sec = int(timestamp) / 1000
        # 时间线由新到旧：早于起点直接结束，晚于终点跳过这一条
        if start_ts and ts_sec < start_ts:
            break
        if end_ts and ts_sec > end_ts:
            continue

        blog_num += 1
        match = _IMG_URL_RE.findall(entry)
        img_url = match[0] if match else ""
        if not img_url:
            continue

        permalink = _PERMALINK_LOOSE_RE.search(entry)
        if not permalink:
            continue
        out.append({
            "img_url": img_url,
            "blog_url": author_url + "post/" + permalink.group(1),
            "time": ts_ms_to_date(timestamp),
        })

    if reporter:
        reporter.log("归档页面解析完毕，共获取博客链接数{}，带图片博客数{}".format(blog_num, len(out)))
    return out


def parse_archive_txt_entries(entries: Iterable[str], author_url: str, author_name: str, *,
                              start_time: str = "",
                              end_time: str = "",
                              reporter=None) -> list[dict]:
    """l9 用途：只保留**文字/文章**博客，产出 ``{url, time, title, print_title, blog_type}``。"""
    from .utils import parse_date_to_ts

    start_ts = parse_date_to_ts(start_time) if start_time else 0.0
    end_ts = parse_date_to_ts(end_time) if end_time else 0.0
    out: list[dict] = []
    blog_num = 0

    for entry in entries:
        try:
            timestamp = _entry_timestamp(entry)
        except ParseError:
            continue
        ts_sec = int(timestamp) / 1000
        if start_ts and ts_sec < start_ts:
            break
        if end_ts and ts_sec > end_ts:
            continue

        blog_num += 1
        title = ""
        titles = _TITLE_RE.findall(entry)
        contents = _CONTENT_RE.findall(entry)
        if titles and titles[0]:
            title = js_unescape_latin(titles[0])
            blog_type = "article"
        elif contents:
            # 没有标题的纯文本，先用占位标题，等拿到时间后再拼
            blog_type = "text"
            title = "tmp_title"
        else:
            # 没有 title 也没有 content：图片博客之类的，跳过
            continue

        permalink = _PERMALINK_STRICT_RE.search(entry)
        if not permalink:
            continue
        dt_time = ts_ms_to_date(timestamp)
        if blog_type == "text":
            title = "{} {}".format(author_name, dt_time)

        out.append({
            "url": author_url + "post/" + permalink.group(1),
            "time": dt_time,
            "title": title,
            "print_title": title.encode("gbk", errors="replace").decode("gbk", errors="replace"),
            "blog_type": blog_type,
        })

    if reporter:
        reporter.log("归档页面解析完毕，共获取博客链接数{}，文本与文章篇数{}".format(blog_num, len(out)))
    return out
