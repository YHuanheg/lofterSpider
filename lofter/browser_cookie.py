"""（可选）从本机浏览器读取 lofter 登录凭证。

参考 `lofter-getter` 的 ``utils/cookie.py``：用 ``browser_cookie3`` 读浏览器 cookie 里的
``LOFTER-PHONE-LOGIN-AUTH``，从而省掉用户 F12 手工复制那一步。

三条自我约束（与参考实现的区别）：

1. **可选依赖**。没装 ``browser_cookie3`` 时 :func:`available` 返回 ``False``，
   界面把按钮置灰并提示安装命令——它绝不能变成第 4 个硬依赖
   （那个包还会拉 ``pycryptodomex`` 一串东西）。
2. **显式触发**。只在用户点「从浏览器读取」时执行，不做启动时静默读取。
3. **只取一条**。全程只在内存里取出 lofter 那一个 cookie 的值，不落盘、不外传。

实测注意：Chrome / Edge **运行时会锁住 cookie 数据库**，读取可能抛
``PermissionError`` / ``sqlite3.OperationalError``。这种情况要提示用户
**完全退出浏览器**再试——这是参考项目 README 没写、但用户一定会遇到的点。
"""

from __future__ import annotations

from typing import Optional

import requests

from .errors import LofterError

__all__ = ["COOKIE_NAME", "BROWSER_CHOICES", "BROWSER_LABELS", "available",
           "read_lofter_auth", "label_of", "PIP_HINT"]

COOKIE_NAME = "LOFTER-PHONE-LOGIN-AUTH"

#: 支持从哪些浏览器读取（与 browser_cookie3 暴露的函数名一致）
BROWSER_CHOICES = (
    "default", "chrome", "edge", "chromium", "brave", "vivaldi",
    "opera", "firefox", "librewolf", "safari",
)

BROWSER_LABELS = {
    "default": "自动（所有浏览器）",
    "chrome": "Chrome",
    "edge": "Edge",
    "chromium": "Chromium",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "firefox": "Firefox",
    "librewolf": "LibreWolf",
    "safari": "Safari",
}

PIP_HINT = "pip install browser-cookie3"


def label_of(browser: Optional[str]) -> str:
    name = str(browser or "default").strip().lower()
    return BROWSER_LABELS.get(name, name)


def available() -> tuple[bool, str]:
    """``browser_cookie3`` 是否可用；不可用时第二个返回值是原因。"""
    try:
        import browser_cookie3  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, str(exc)
    return True, ""


def read_lofter_auth(browser: str = "default") -> str:
    """从浏览器读出 ``LOFTER-PHONE-LOGIN-AUTH``。

    失败时抛 :class:`LofterError`，消息里一定包含「下一步该做什么」。
    """
    usable, reason = available()
    if not usable:
        raise LofterError(
            "这个功能需要额外安装一个包：\n    {}\n"
            "（原因：{}）\n"
            "装好之后重新打开程序即可；不装也不影响其它功能，"
            "手工复制 login_auth 依然可用。".format(PIP_HINT, reason))

    import browser_cookie3

    name = str(browser or "default").strip().lower()
    where = label_of(name)
    try:
        if name == "default":
            jar = browser_cookie3.load()
        else:
            loader = getattr(browser_cookie3, name, None)
            if loader is None:
                raise LofterError("不支持从「{}」读取，可选：{}".format(
                    browser, " / ".join(BROWSER_CHOICES)))
            jar = loader()
    except LofterError:
        raise
    except Exception as exc:
        raise LofterError(
            "从「{}」读取 cookie 失败：{}：{}\n"
            "常见原因：\n"
            "1) 浏览器正在运行，cookie 数据库被锁住 → **完全退出浏览器**后重试；\n"
            "2) 该浏览器里没有登录过 lofter。".format(where, type(exc).__name__, exc)) from exc

    values = requests.utils.dict_from_cookiejar(jar)
    auth = str(values.get(COOKIE_NAME) or "").strip()
    if not auth:
        raise LofterError(
            "在「{}」里没找到 {}。\n"
            "请先在那个浏览器里登录 lofter，再回来点一次；"
            "或者换成「自动（所有浏览器）」试试。".format(where, COOKIE_NAME))
    return auth
