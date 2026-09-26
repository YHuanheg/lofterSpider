"""（可选）把合集导出为 EPUB。

参考 `lofter-getter` 的 ``utils/epub.py``，它里面有**两条踩坑才得到的经验**值得直接采纳，
外加我们自己在实测中踩到的第三条：

1. **章节文件名必须用序号**（``chapter_1.xhtml``），不能用标题——
   标题里的特殊字符会破坏 EPUB 打包。这正是它 2026-08 那条 PR 修的东西。
2. **nav 不进 spine**，否则部分阅读器打开是空白页。
3. **``EpubHtml.content`` 的契约是「正文片段」，不是完整 XHTML 文档**。
   塞完整文档进去，ebooklib 内部 ``parse_html_string`` 会失败、
   ``get_content()`` 静默返回空字节 —— 表现是「epub 生成成功、每章都是 0 字节」。
   （这个坑是我们实测抓到的：先 zipfile 读回章节内容才发现的。）

实现路径也与它不同：它走 ``md → markdown.markdown() → html``，因此要多引入 ``markdown`` 依赖。
本项目手上本来就有章节的**原始 HTML** 和 ``lxml``，可以直接规范化成片段塞进章节，
**省掉 ``markdown`` 这个依赖**。

``ebooklib`` 依旧是可选依赖：没装时 :func:`available` 返回 ``False``，
合集任务会把「导出 EPUB」开关跳过并给出提示，不影响其它功能。
"""

from __future__ import annotations

import os
import re
from html import escape
from typing import Iterable, Sequence

from lxml import etree

from .errors import LofterError

__all__ = ["available", "export_epub", "build_xhtml", "normalize_fragment",
           "media_type", "PIP_HINT"]

PIP_HINT = "pip install EbookLib"

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

XHTML_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">\n'
    '<head>\n'
    '  <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>\n'
    '  <title>{title}</title>\n'
    '</head>\n'
    '<body>\n'
    '{body}\n'
    '</body>\n'
    '</html>\n'
)


def available() -> tuple[bool, str]:
    """``ebooklib`` 是否可用；不可用时第二个返回值是原因。"""
    try:
        import ebooklib  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, str(exc)
    return True, ""


def media_type(path: str) -> str:
    return MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "image/jpeg")


def normalize_fragment(fragment: str) -> str:
    """把一段 HTML 规范化成**良构的正文片段**（不含 ``<html>`` / ``<body>`` 外壳）。

    lofter 的正文 HTML 经常不闭合（``<br>``、``<img>``、缺结束标签），
    直接塞进 EPUB 会让严格校验的阅读器报错，所以统一过一遍 lxml，
    顺带把 ``<br>`` 这类补成自闭合形式。

    注意实现细节：``etree.fromstring(..., HTMLParser())`` 返回的是 **``<html>`` 根元素**
    而不是我们套的那层 ``<div>``（HTML 解析器总会补齐 html/body），
    所以要先 ``.find(".//div")`` 拿回自己那层包装，再只序列化它的**子节点**
    （连同尾随文本），这样才真的不带外壳。

    万一连 lxml 都吃不下（极端脏数据），退化成转义后的纯文本——
    **绝不因为一章脏数据让整本书导出失败**。
    """
    body = str(fragment or "")
    if not body.strip():
        return ""
    try:
        root = etree.HTML("<div>{}</div>".format(body))
        node = root.find(".//div") if root is not None else None
        if node is not None:
            parts = [node.text or ""]
            for child in node:
                parts.append(etree.tostring(child, encoding="unicode", method="xml"))
            inner = "".join(parts).strip()
            if inner:
                return inner
    except Exception:  # noqa: BLE001 - 脏数据兜底
        pass
    return "<pre>{}</pre>".format(escape(body))


def build_xhtml(fragment: str, title: str, lang: str = "zh-CN") -> str:
    """拼出一份**完整 XHTML 文档**（独立查看 / 结构校验用）。

    注意：这个函数的结果**不能**直接赋给 ``EpubHtml.content``——那里要的是片段，
    见模块开头第 3 条经验。EPUB 内部请用 :func:`normalize_fragment`。
    """
    return XHTML_TEMPLATE.format(lang=lang, title=escape(title or ""),
                                 body=normalize_fragment(fragment))


def export_epub(chapters: Sequence[dict], out_path: str, *,
                title: str, author: str = "", language: str = "zh-CN",
                images: Iterable[tuple] = ()) -> str:
    """写出一本 EPUB。

    :param chapters: ``[{"title": str, "html": str}, ...]``，按顺序即章节顺序。
    :param images: ``[(epub 内的相对路径, 磁盘绝对路径), ...]``，
        例如 ``("images/001-01.jpg", "/…/images/001-01.jpg")``。
    :return: 实际写出的文件路径。

    章节文件名固定为 ``chapter_<序号>.xhtml``（见模块开头第 1 条经验）；
    章节的显示标题仍然是真实标题，读者看到的是标题、包内是序号。
    """
    usable, reason = available()
    if not usable:
        raise LofterError(
            "导出 EPUB 需要额外安装一个包：\n    {}\n（原因：{}）".format(PIP_HINT, reason))

    from ebooklib import epub

    chapters = [c for c in chapters if c]
    if not chapters:
        raise LofterError("没有可写入 EPUB 的章节")

    book = epub.EpubBook()
    book.set_identifier("lofter-collection-{:08x}".format(abs(hash(title or "")) & 0xFFFFFFFF))
    book.set_title(title or "未命名合集")
    book.set_language(language)
    if author:
        book.add_author(author)

    # 图片先登记，章节里的相对引用（images/xxx.jpg）才能被解析到
    seen_images: set = set()
    for relative, absolute in images or ():
        key = str(relative).replace("\\", "/")
        if key in seen_images or not os.path.exists(absolute):
            continue
        seen_images.add(key)
        with open(absolute, "rb") as fp:
            data = fp.read()
        book.add_item(epub.EpubImage(
            uid="img_{}".format(re.sub(r"\W+", "_", key)),
            file_name=key, media_type=media_type(key), content=data))

    epub_chapters = []
    for index, chapter in enumerate(chapters, 1):
        chapter_title = str(chapter.get("title") or "第 {} 章".format(index))
        body = normalize_fragment(str(chapter.get("html") or ""))
        item = epub.EpubHtml(
            title=chapter_title,
            file_name="chapter_{}.xhtml".format(index),   # ← 序号，不用标题
            lang=language)
        # content 必须是**片段**，ebooklib 自己会补 html/head/body 外壳
        item.content = "<h1>{}</h1>\n{}".format(escape(chapter_title), body)
        book.add_item(item)
        epub_chapters.append(item)

    book.toc = tuple(
        epub.Link(item.file_name, item.title, "chap_{}".format(i))
        for i, item in enumerate(epub_chapters, 1))
    # nav 不进 spine：部分阅读器会因此打开空白页（参考实现的修补结论）
    book.spine = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # 必须显式关掉 epub3_pages：它内部会走 ebooklib 的 get_pages()，对我们生成的
    # XHTML 抛 `lxml.etree.ParserError: Document is empty`，整本书写不完。
    # 参考项目在生产里也是这么干的（{'ignore_ncx': False, 'epub3_pages': False}），
    # 它那个版本甚至还要兼容老版 ebooklib 不接受选项字典的情况。
    try:
        epub.write_epub(out_path, book, {"ignore_ncx": False, "epub3_pages": False})
    except TypeError:
        epub.write_epub(out_path, book)
    return out_path
