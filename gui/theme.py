"""浅色 / 深色 / 跟随系统 三套配色。

参考另一个项目（wallpaper）的做法：工具栏上放一个「主题」切换器，切换后
**立即生效并持久化**。那边用的是 customtkinter，自带 ``set_appearance_mode``；
标准库 ``ttk`` 没有等价能力，所以这里自己维护调色板，并负责把 ttk 的
``clam`` 主题按调色板整体配置一遍。

**为什么必须切到 ``clam``**：Windows 默认的 ``vista`` / ``wpnative`` 主题由系统
原生绘制，``style.configure`` 改不动颜色。切深色时如果不换主题，会得到
「深色窗口 + 亮色按钮」的割裂效果，比不做还难看。

**为什么 ttk 之外还要单独处理**：``tk.Canvas`` / ``tk.Text`` / ``tk.Listbox``
不是 ttk 控件，不吃 style，必须在切换时逐个 ``configure``。所以
:class:`ThemeManager` 维护一串「关注主题的组件」回调，切换时统一通知。
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["THEME_VALUES", "resolve_mode", "palette_of", "ThemeManager"]

#: 界面上可选的模式
THEME_VALUES = ("跟随系统", "浅色", "深色")

_MODE_ALIASES = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
    "跟随系统": "跟随系统",
    "浅色": "浅色",
    "深色": "深色",
}

_LIGHT = {
    "mode": "浅色",
    "bg": "#f7f8fa",           # 窗口底色
    "panel": "#ffffff",        # 卡片 / 输入框底色
    "panel_alt": "#eef0f4",    # 次级面板（表头、条纹）
    "text": "#1f2328",
    "muted": "#6b7280",
    "accent": "#2f6feb",
    "accent_text": "#ffffff",
    "border": "#c9ced6",
    "trough": "#e4e7ec",
    "list_bg": "#ffffff",
    "list_select_bg": "#dbe7ff",
    "list_select_fg": "#12305f",
    "log_debug": "#8b95a1",
    "log_info": "#1f2328",
    "log_warn": "#b7791f",
    "log_error": "#c53030",
    "log_bg": "#ffffff",
    "log_select": "#dbe7ff",
}

_DARK = {
    "mode": "深色",
    "bg": "#1e1f22",
    "panel": "#2b2d31",
    "panel_alt": "#35373c",
    "text": "#e6e6e6",
    "muted": "#9aa0a6",
    "accent": "#4c8dff",
    "accent_text": "#0b1220",
    "border": "#4a4d52",
    "trough": "#3a3d42",
    "list_bg": "#2b2d31",
    "list_select_bg": "#33456b",
    "list_select_fg": "#e8efff",
    "log_debug": "#8b95a1",
    "log_info": "#e6e6e6",
    "log_warn": "#e3b341",
    "log_error": "#ff7b72",
    "log_bg": "#232527",
    "log_select": "#33456b",
}

_PALETTES = {"浅色": _LIGHT, "深色": _DARK}


def system_prefers_dark() -> bool:
    """Windows 上读注册表判断系统是否使用深色模式，其它平台或读取失败按浅色。"""
    try:  # pragma: no cover - 平台相关
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def resolve_mode(value: Optional[str]) -> str:
    """把任意写法（``dark`` / ``深色`` / ``跟随系统``）归一成界面上的三种之一。"""
    if not value:
        return "跟随系统"
    return _MODE_ALIASES.get(str(value).strip(), "跟随系统")


def palette_of(value: Optional[str]) -> dict:
    """取某个模式对应的调色板（``跟随系统`` 会去问操作系统）。"""
    mode = resolve_mode(value)
    if mode == "跟随系统":
        mode = "深色" if system_prefers_dark() else "浅色"
    return _PALETTES[mode]


class ThemeManager:
    """管住 ttk 全局样式 + 一批需要手动重绘的组件。"""

    def __init__(self, root, mode: str = "跟随系统") -> None:
        self.root = root
        self.mode = resolve_mode(mode)
        self.palette: dict = palette_of(self.mode)
        self._watchers: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------
    def watch(self, callback: Callable[[dict], None]) -> None:
        """注册一个「配色变了请重绘」的回调。"""
        if callback not in self._watchers:
            self._watchers.append(callback)

    def set_mode(self, value: str) -> str:
        """切换模式并立刻生效，返回归一化后的模式名。"""
        self.mode = resolve_mode(value)
        self.palette = palette_of(self.mode)
        self.apply()
        return self.mode

    def refresh(self) -> None:
        """重新解析（用于「跟随系统」时系统主题变了、或窗口刚建好要补一次）。"""
        if self.mode == "跟随系统":
            self.palette = palette_of(self.mode)
            self.apply()

    # ------------------------------------------------------------------
    def apply(self) -> None:
        self._apply_ttk()
        for callback in list(self._watchers):
            try:
                callback(self.palette)
            except Exception:  # pragma: no cover - 组件可能已被销毁
                pass

    def _apply_ttk(self) -> None:
        from tkinter import ttk

        p = self.palette
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:  # pragma: no cover - 极端环境下没有 clam
            pass

        style.configure(".", background=p["bg"], foreground=p["text"],
                        fieldbackground=p["panel"], bordercolor=p["border"],
                        lightcolor=p["panel"], darkcolor=p["panel"],
                        troughcolor=p["trough"], focuscolor=p["accent"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        style.map("TCheckbutton", background=[("active", p["bg"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
        style.configure("TButton", background=p["panel_alt"], foreground=p["text"],
                        bordercolor=p["border"], padding=(10, 4))
        style.map("TButton",
                  background=[("active", p["accent"]), ("disabled", p["panel_alt"])],
                  foreground=[("active", p["accent_text"]), ("disabled", p["muted"])])
        style.configure("TEntry", fieldbackground=p["panel"], foreground=p["text"],
                        insertcolor=p["text"], bordercolor=p["border"], padding=3)
        style.map("TEntry", fieldbackground=[("readonly", p["panel_alt"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TSpinbox", fieldbackground=p["panel"], foreground=p["text"],
                        insertcolor=p["text"], arrowcolor=p["text"], padding=3)
        style.configure("TCombobox", fieldbackground=p["panel"], background=p["panel_alt"],
                        foreground=p["text"], arrowcolor=p["text"], bordercolor=p["border"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", p["panel"]), ("disabled", p["panel_alt"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                        relief="solid")
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                        tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", background=p["panel_alt"], foreground=p["muted"],
                        padding=(14, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", p["bg"])],
                  foreground=[("selected", p["text"])])
        style.configure("TProgressbar", background=p["accent"], troughcolor=p["trough"],
                        bordercolor=p["border"], lightcolor=p["accent"],
                        darkcolor=p["accent"])
        style.configure("TPanedwindow", background=p["bg"])
        style.configure("Sash", background=p["border"])
        style.configure("Vertical.TScrollbar", background=p["panel_alt"],
                        troughcolor=p["bg"], arrowcolor=p["text"],
                        bordercolor=p["border"])
        style.configure("Horizontal.TScrollbar", background=p["panel_alt"],
                        troughcolor=p["bg"], arrowcolor=p["text"],
                        bordercolor=p["border"])
        style.configure("TSeparator", background=p["border"])

        # 下拉框弹出列表是原生 Tk listbox，只能走 option 数据库
        for pattern, value in (
            ("*TCombobox*Listbox.background", p["panel"]),
            ("*TCombobox*Listbox.foreground", p["text"]),
            ("*TCombobox*Listbox.selectBackground", p["list_select_bg"]),
            ("*TCombobox*Listbox.selectForeground", p["list_select_fg"]),
        ):
            try:
                self.root.option_add(pattern, value)
            except Exception:  # pragma: no cover
                pass

        try:
            self.root.configure(background=p["bg"])
        except Exception:  # pragma: no cover
            pass
