"""通用工具：文件名安全化、时间换算、JS 字符串反转义、图片链接过滤、tag 过滤。

集中了原来被复制了 5 份的字符串清洗逻辑（l4 / l8 / l9 / l10 / l13 各一份），
以及 l4/l8/l9 共用的图片链接过滤与 tag 过滤。

.. note:: 关于「保持原样」的取舍
   凡是会改变**输出文件名**或**解析结果**的写法，本模块一律按原脚本保留原语义，
   只在明显是笔误（例如 ``decode(errors="ignore ")`` 多了个空格）或明显是
   Windows 编码 bug 的地方才修，并且都在注释里标出来。
"""

from __future__ import annotations

import os
import re
import time
from typing import Iterable, Sequence

__all__ = [
    "sanitize_filename",
    "sanitize_path_part",
    "dedup_filename",
    "parse_date_to_ts",
    "ts_ms_to_date",
    "ts_to_datetime_str",
    "now_ts_ms",
    "detect_img_type",
    "is_lofter_img",
    "filter_img_urls",
    "pick_best_img_url",
    "tag_match",
    "split_tags",
    "normalize_tags",
    "js_unescape_latin",
    "js_unescape_utf8",
    "literal_list",
    "safe_makedirs",
    "iter_in_chunks",
]

# Windows 文件名禁用字符 → 替代字符。
# 原脚本把半角括号换成全角括号，是为了让 dedup_filename 能用 "(" 做重名后缀分隔符。
_ILLEGAL_MAP = (
    ("/", "&"),
    ("|", "&"),
    ("\\", "&"),
    ("<", "《"),
    (">", "》"),
    (":", "："),
    ('"', "”"),
    ("?", "？"),
    ("*", "·"),
    ("(", "（"),
    (")", "）"),
    ("\r", " "),
    ("\t", " "),
    ("\n", ""),
)

# 控制字符（0x00-0x08 / 0x0b-0x0c / 0x0e-0x1f）连同 \t \n \r 之外的都换成空格
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

# 「新格式」站内图链接。注意 ``.`` 在原脚本里没有转义，这里保留原语义不动，
# 因为把 dot 收紧成 ``\.`` 有可能让某些历史链接匹配失败。
_LOFTER_IMG_NEW_RE = re.compile(r"http[s]{0,1}://imglf\d{0,1}.lf\d*.[0-9]{0,3}.net")
# 「旧格式」站内图链接（2020 年 9 月之前）
_LOFTER_IMG_OLD_RE = re.compile(r"http[s]{0,1}://imglf\d{0,1}.nosdn\d*.[0-9]{0,3}.net")


# --------------------------------------------------------------------- 文件名
def sanitize_filename(name: str, fullwidth_comma: bool = False) -> str:
    """把任意字符串变成 Windows 上安全的文件名。

    :param fullwidth_comma: l9 额外把半角逗号也换成全角，为保持既有输出文件名不变
        而保留这个开关。
    """
    if not name:
        return ""
    out = str(name)
    for bad, good in _ILLEGAL_MAP:
        out = out.replace(bad, good)
    if fullwidth_comma:
        out = out.replace(",", "，")
    return _CONTROL_RE.sub(" ", out).strip()


def sanitize_path_part(name: str) -> str:
    """清洗一个用作目录名分段的 tag。"""
    return sanitize_filename(name)


def dedup_filename(filename: str, content, path: str, file_type: str) -> str:
    """同名文件查重（原 ``filename_check``）。

    规则：目标文件不存在 → 用原名；存在且内容完全相同 → 用原名（幂等覆盖）；
    存在但内容不同 → 依次尝试 ``name(2).ext`` ``name(3).ext`` …

    :param content: ``file_type == "txt"`` 时是 ``str``，否则是 ``bytes``。
    """
    full = os.path.join(path, filename)
    if not os.path.exists(full):
        return filename
    if _read_existing(full, file_type) == content:
        return filename

    stem = filename.split("." + file_type)[0].split("(")[0]
    num = 2
    while True:
        candidate = "{}({}).{}".format(stem, num, file_type)
        full = os.path.join(path, candidate)
        if os.path.exists(full):
            if _read_existing(full, file_type) == content:
                return candidate
            num += 1
        else:
            return candidate


def _read_existing(full_path: str, file_type: str):
    """读已存在文件内容用于比对。**修 bug**：原代码读 txt 时声明了 utf-8，
    但读图片时没声明；这里两边都显式声明，避免 Windows 上按 GBK 读出错。"""
    if file_type == "txt":
        with open(full_path, "r", encoding="utf-8", errors="replace") as fp:
            return fp.read()
    with open(full_path, "rb") as fp:
        return fp.read()


# ----------------------------------------------------------------------- 时间
def parse_date_to_ts(date_str: str) -> float:
    """``"2020-02-01"`` → 本地时区当天 0 点的 POSIX 时间戳。"""
    return time.mktime(time.strptime(date_str.strip(), "%Y-%m-%d"))


def ts_ms_to_date(ts_ms) -> str:
    """毫秒时间戳 → ``"YYYY-MM-DD"``。"""
    return time.strftime("%Y-%m-%d", time.localtime(int(int(ts_ms) / 1000)))


def ts_to_datetime_str(ts_ms) -> str:
    """毫秒时间戳 → ``"YYYY-MM-DD HH:MM"``（评论用）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts_ms) / 1000))


def now_ts_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------- 图片链接
def detect_img_type(img_url: str) -> str:
    """按链接猜图片扩展名，兼容原逻辑（gif > png > jpg）。"""
    if re.findall("gif", img_url):
        return "gif"
    if re.findall("png", img_url):
        return "png"
    return "jpg"


def is_lofter_img(img_url: str, accept_old: bool = False) -> bool:
    """是否是 lofter 站内图（决定要不要带 Referer 下载）。"""
    if _LOFTER_IMG_NEW_RE.match(img_url):
        return True
    if accept_old and _LOFTER_IMG_OLD_RE.match(img_url):
        return True
    return False


def filter_img_urls(imgs_url: Iterable[str], blog_type: str) -> list[str]:
    r"""过滤头像/推荐位小图并去重（原 ``img_fliter``）。

    * ``&amp;`` 在 ``img`` 类型里直接丢弃；在 text/article 里只有形如 ``\d\d&amp`` 才丢；
    * 形如 ``16x16`` / ``49y49`` 的缩略图参数直接丢；
    * 去掉 ``imageView`` 之后的裁剪参数以取最高画质；
    * 保序去重。
    """
    out: list[str] = []
    for img_url in imgs_url:
        if "&amp;" in img_url:
            if blog_type == "img":
                continue
            if re.search(r"\d\d&amp", img_url):
                continue
        if re.search(r"[1649]{2}[x,y][1649]{2}", img_url):
            continue
        cleaned = img_url.split("imageView")[0]
        if cleaned not in out:
            out.append(cleaned)
    return out


def pick_best_img_url(url_info: dict) -> str:
    """从 ``originPhotoLinks`` 的单个元素里选最优原图链接。

    优先 ``raw``；没有 ``raw`` 或者链接里带 ``netease``（网易图床另需处理）时，
    退回 ``orign`` 并砍掉 ``?imageView`` 裁剪参数。
    """
    url = url_info.get("raw", "")
    if not url or "netease" in url:
        url = url_info.get("orign", "").split("?imageView")[0]
    return url


# --------------------------------------------------------------------- tag
def normalize_tags(raw_tags: Sequence[str]) -> list[str]:
    """tag 统一转小写、全角空格转半角（原 l13 的 lower_tags 逻辑）。"""
    return [t.lower().replace("\u3000", " ").strip() for t in raw_tags]


def split_tags(raw: str) -> list[str]:
    """把 DWR 里的 tag 字符串按逗号切开，空串视为没有 tag。"""
    if not raw:
        return []
    parts = raw.strip().split(",")
    if parts and parts[0] == "":
        return []
    return parts


def tag_match(blog_tags: Sequence[str], target_tags: Sequence[str], mode: str) -> bool:
    """tag 过滤（原 ``tag_filter``）。

    :param mode: ``"in"`` → 保留命中目标 tag 的博客，且**没有 tag 的博客也保留**；
        ``"out"`` → 没有 tag 的博客会被过滤掉。
    """
    if not target_tags:
        return True
    if not blog_tags:
        return mode == "in"
    for tag in blog_tags:
        if tag in target_tags:
            return True
    return False


# --------------------------------------------------------- DWR / JS 字符串
def js_unescape_latin(text: str) -> str:
    """还原 DWR 响应里的 JS 字符串转义（作者名 / 标题 / 正文）。

    原理与原脚本一致：``str.encode("latin-1")`` 保证字节不变，
    再用 ``unicode_escape`` 把 ``\\uXXXX`` / ``\\xXX`` 还原。

    **修 bug**：原代码在标题上写成 ``decode("unicode_escape", errors="ignore ")``
    （handler 名多了个尾空格），一旦真的触发错误分支会抛 ``LookupError``，
    被裸 ``except`` 吞掉后标题变成空串，进而把文章误判成文本。
    这里改成正确的 ``errors`` 名，并在 ``latin-1`` 编码失败（源码里含未转义的非 ASCII）
    时**退回原串**而不是丢掉内容。
    """
    if not text:
        return ""
    try:
        return text.encode("latin-1").decode("unicode_escape", errors="replace")
    except UnicodeEncodeError:
        return text


def js_unescape_utf8(text: str) -> str:
    """同上，但走 ``encode("utf-8")``。用于 tag 串与评论正文（原脚本如此）。"""
    if not text:
        return ""
    try:
        return text.encode("utf-8").decode("unicode_escape", errors="replace")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def literal_list(raw: str) -> list:
    """把 ``originPhotoLinks="[...]"`` 里的字面量安全地转成 Python 列表。

    **安全修复**：原代码用 ``eval()`` 执行抓取回来的字符串，等于把
    远端内容当代码跑。要素只是 list/dict/str/number，``ast.literal_eval`` 足够。
    """
    import ast

    if not raw:
        return []
    normalized = raw.replace("\\", "").replace("false", "False").replace("true", "True")
    try:
        value = ast.literal_eval(normalized)
    except (ValueError, SyntaxError):
        return []
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------- 杂项
def safe_makedirs(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path)


def iter_in_chunks(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
