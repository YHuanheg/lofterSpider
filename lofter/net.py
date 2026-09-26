"""HTTP 层：Session 构造、登录、DWR 调用、页面/图片获取。

原代码里每个脚本各写一份请求头、各自处理 cookies。这里统一，并修掉两个真实缺陷：

1. ``useragentutil.get_headers()`` 原来返回的是**列表里那个 dict 本身**，调用方
   ``tmp_headers["Referer"] = ...`` 会永久污染全局常量，导致后续请求带上上一个
   作者的 Referer。这里返回**新字典副本**。
2. 请求返回非 2xx 时原代码一路往下走，最后在解析阶段报一个看不懂的错。
   这里显式抛 :class:`NetworkError`。
"""

from __future__ import annotations

import random
import time
from typing import Mapping, Optional

import requests
from lxml.html import etree

from .errors import AuthError, NetworkError

__all__ = [
    "USER_AGENTS",
    "get_headers",
    "make_session",
    "login_session",
    "extract_blog_id",
    "post_dwr",
    "get_text",
    "fetch_bytes",
    "polite_sleep",
]

USER_AGENTS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    },
]

LOFTER_HOST = "www.lofter.com"
LOGIN_PAGE_URL = "http://www.lofter.com/login"


def get_headers(extra: Optional[Mapping[str, str]] = None) -> dict:
    """取一份请求头。**返回副本**，调用方随便改都不会影响别人。"""
    base = dict(random.choice(USER_AGENTS))
    if extra:
        base.update(extra)
    return base


def make_session(referer: Optional[str] = None,
                 host: Optional[str] = None,
                 cookies: Optional[Mapping[str, str]] = None) -> requests.Session:
    """建一个带标准请求头的 Session。"""
    session = requests.Session()
    session.headers = get_headers({
        "Host": host or LOFTER_HOST,
        **({"Referer": referer} if referer else {}),
    })
    if cookies:
        session.cookies.update(dict(cookies))
    return session


def login_session(login_key: str, login_auth: str, reporter=None) -> requests.Session:
    """走一次 ``lofter.com/login`` → 首页，拿到带登录 cookie 的 Session。

    等价于原 ``get_logion_session``。失败时抛 :class:`AuthError`（原代码是 ``exit()``）。
    """
    if not login_auth:
        raise AuthError("登录信息为空：请先在「登录信息」里填入 login_auth。"
                        "获取方式见 README 的『填写登录信息』一节。")

    session = requests.Session()
    session.headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/79.0.3945.88 Safari/537.36",
        "Host": LOFTER_HOST,
    }
    try:
        resp = session.get(LOGIN_PAGE_URL, params={"urschecked": "true"}, timeout=20)
    except requests.RequestException as exc:
        raise NetworkError("访问登录页失败：{}".format(exc)) from exc
    if reporter:
        reporter.debug("登录页状态码 {}".format(resp.status_code))

    session.headers["Referer"] = LOGIN_PAGE_URL
    session.cookies.set(login_key, login_auth)

    try:
        resp = session.get("http://www.lofter.com/", timeout=20)
    except requests.RequestException as exc:
        raise NetworkError("访问 lofter 首页失败：{}".format(exc)) from exc
    if reporter:
        reporter.debug("主页请求状态码 {}".format(resp.status_code))
    if resp.status_code >= 400:
        raise NetworkError("lofter 首页返回 {}，可能是网络被拦截或需要代理".format(resp.status_code))
    return session


def extract_blog_id(html: str, *, what: str = "用户主页") -> str:
    """从 ``<iframe id="control_frame" src="...blogId=xxx">`` 里抠出数字 id。

    拿不到通常意味着没登录（或 cookie 过期），抛 :class:`AuthError`。
    """
    try:
        page = etree.HTML(html)
        src = page.xpath("//body//iframe[@id='control_frame']/@src")[0]
        return src.split("blogId=")[1]
    except (IndexError, AttributeError, TypeError) as exc:
        raise AuthError(
            "{}登录验证失败，拿不到 blogId。请检查 login_key/login_auth 是否有效、是否过期。".format(what)
        ) from exc


def post_dwr(session: requests.Session, url: str, data: Mapping[str, str],
             timeout: int = 30, verify: bool = True) -> str:
    """POST 一个 DWR 接口并返回 utf-8 正文。"""
    try:
        resp = session.post(url, data=data, timeout=timeout, verify=verify)
    except requests.RequestException as exc:
        raise NetworkError("请求 {} 失败：{}".format(url, exc)) from exc
    if resp.status_code >= 400:
        raise NetworkError("{} 返回状态码 {}".format(url, resp.status_code))
    return resp.content.decode("utf-8", errors="replace")


def get_text(session: Optional[requests.Session], url: str, *,
             referer: Optional[str] = None,
             cookies: Optional[Mapping[str, str]] = None,
             timeout: int = 30) -> str:
    """GET 一个 HTML 页面并返回 utf-8 正文。

    给了 ``session`` 就走 session（沿用它已经设好的 Host/UA），否则用一套新请求头。
    """
    headers = {"Referer": referer} if referer else None
    try:
        if session is not None:
            resp = session.get(url, headers=headers, cookies=cookies, timeout=timeout)
        else:
            resp = requests.get(url, headers=get_headers(headers), cookies=cookies,
                                timeout=timeout)
    except requests.RequestException as exc:
        raise NetworkError("请求 {} 失败：{}".format(url, exc)) from exc
    if resp.status_code >= 400:
        raise NetworkError("{} 返回状态码 {}".format(url, resp.status_code))
    return resp.content.decode("utf-8", errors="replace")


def fetch_bytes(session: Optional[requests.Session], url: str, *,
                referer: Optional[str] = None,
                cookies: Optional[Mapping[str, str]] = None,
                timeout: int = 60) -> bytes:
    """下载二进制内容（图片）。

    lofter 的 ``imglf`` 图床对部分链接校验 Referer，所以调用方应尽量给 referer。
    """
    headers = get_headers({"Referer": referer} if referer else None)
    try:
        if session is not None:
            resp = session.get(url, headers=headers, cookies=cookies, timeout=timeout)
        else:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=timeout)
    except requests.RequestException as exc:
        raise NetworkError("下载 {} 失败：{}".format(url, exc)) from exc
    if resp.status_code >= 400:
        raise NetworkError("下载 {} 返回状态码 {}".format(url, resp.status_code))
    return resp.content


def polite_sleep(lo: float = 1.0, hi: float = 2.0, reporter=None) -> None:
    """随机 sleep，降低被限流的概率。被取消时可提前跳出。"""
    delay = random.uniform(lo, hi) if hi > lo else lo
    if reporter is not None:
        end = time.time() + delay
        while time.time() < end:
            reporter.check_cancel()
            time.sleep(min(0.2, end - time.time()))
    else:
        time.sleep(delay)
