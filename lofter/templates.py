"""正文模板匹配（原 ``parse_template.py``）。

原脚本会在候选模板中找到第一个「非空」的作为该作者主页的模板，然后对它所有博客
都用同一个模板抽正文。改动点：

* 所有正则改成 **raw string**，消掉 Python 3.12 的 ``SyntaxWarning: invalid escape sequence``；
* 删掉 ``all_purpose_template`` 里顺手写 ``test.txt`` 的副作用；
* ``title.encode("utf-8", errors="replace").decode("utf-8", errors="replace")`` 是恒等变换，
  保留语义但简化为直接使用。
"""

from __future__ import annotations

import re

__all__ = ["matcher", "get_content", "TEMPLATE_IDS"]

# 通用模板的兜底 id
TEMPLATE_GENERIC = 0
TEMPLATE_IDS = (1, 2, 3, 4, 5, 6, 7)


# 通用模板，会爬到些别的
def all_purpose_template(parse, title, blog_type, join_word=""):
    lines = parse.xpath('/html//text()')
    content = join_word.join(lines)
    if blog_type == "article":
        try:
            content = content.split(title, 2)[2]
        except (IndexError, ValueError):
            pass
        content = re.split(r"\s评论\s", content)[0]
    else:
        content = content.split("评论")[0]
    return content


# 模板1 lofter初始模板 http://yangliu12.lofter.com 有标题
def template1(parse, join_word=""):
    lines = parse.xpath('//div[@class="content"]/div[@class="text"]//text()')
    return join_word.join(lines)


# 模板2 http://sxhyl.lofter.com/post/1e77aca2_1c6d7acdc 有标题
def template2(parse, join_word=""):
    lines = parse.xpath('//div[@class="cont"]/div[@class="text"]//text()')
    return join_word.join(lines)


# 模板3 https://bmdxc.lofter.com/post/3d8916_1c9a35a4b 有标题，跟2很像，但是标签有点问题
def template3(parse, join_word=""):
    lines = parse.xpath('//div[@class="cont"]/div[@class]//text()')
    return join_word.join(lines).split("评论")[0]


# 模板4 http://cersternay.lofter.com/post/1d57590b_ee734b04 无标题
def template4(parse, join_word=""):
    lines = parse.xpath('//div[@class="txtcont"]//text()')
    return join_word.join(lines)


# 模板5 https://imakuf.lofter.com/post/1f7d9e_1c7651049 无标题
def template5(parse, join_word=""):
    lines = parse.xpath('//div[@class="text"]//text()')
    return join_word.join(lines)


# 模板6 https://anisette642.lofter.com/post/30f2af97_1c9a05b43 有标题
def template6(parse, join_word=""):
    lines = parse.xpath('//div[@class="text"]/p/text()')
    return (join_word + "\n\n").join(lines)


# 模板7 https://chuanshoot.lofter.com/ 好像是自定义主页
def template7(parse, join_word=""):
    lines = parse.xpath("//div[contains(@class,'post-ctc box')]//p//text()")
    return join_word.join(lines)


_TEMPLATES = {
    1: template1,
    2: template2,
    3: template3,
    4: template4,
    5: template5,
    6: template6,
    7: template7,
}


def matcher(parse) -> int:
    """按「第一个非空模板」规则选模板，全空则返回 ``0``（通用模板）。"""
    for template_id in TEMPLATE_IDS:
        if _TEMPLATES[template_id](parse) != "":
            return template_id
    return TEMPLATE_GENERIC


def get_content(parse, template_id, title, blog_type, join_word="") -> str:
    """用指定模板取正文。1/2/3 号模板会顺手删掉开头重复的标题。"""
    if template_id in _TEMPLATES:
        content = _TEMPLATES[template_id](parse, join_word)
        if template_id in (1, 2, 3) and title:
            content = content.replace(title, "", 1)
    else:
        content = all_purpose_template(parse, title, blog_type, join_word)
        content = content.replace("    ", "").replace("\t", "")
    return content.strip()
