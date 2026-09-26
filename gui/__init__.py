"""图形界面包。

单独提供 :func:`launch`，方便 ``main.py`` 在 tkinter 不可用时优雅降级到 CLI。
"""

from __future__ import annotations

__all__ = ["launch", "tk_available"]


def tk_available() -> tuple[bool, str]:
    """检查 tkinter 是否可用（部分精简版 Python 不带 tcl/tk）。"""
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, str(exc)
    return True, ""


def launch() -> int:
    ok, reason = tk_available()
    if not ok:
        print("当前 Python 没有可用的 tkinter，无法启动图形界面：{}".format(reason))
        print("解决办法：安装带 tcl/tk 的官方 Python，或改用命令行模式（python main.py --list）。")
        return 3
    from .app import main as app_main

    return app_main()
