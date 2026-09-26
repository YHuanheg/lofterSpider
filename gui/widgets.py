"""tkinter 界面组件：可滚动表单、日志面板、登录信息弹窗。

界面完全由 ``Field`` 声明生成，所以**新增一个抓取任务不需要写任何界面代码**——
只要给它的配置类加上 ``FIELDS``，表单就会自动出现。

表单读取值时直接把原始字符串交给 ``Field.coerce``，校验逻辑只有一份，
出错时用 ``ConfigError`` 的中文提示弹窗，不用在界面层重复写校验。

配色不写死在这里：每个组件都提供 ``apply_palette(p)``，由 :class:`gui.theme.ThemeManager`
在切换主题时统一调用（``tk.Canvas`` / ``tk.Text`` / ``tk.Listbox`` 不是 ttk 控件，
不吃 style，必须显式重绘）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from lofter.config import Account, ConfigBase, Field, LOGIN_KEY_CHOICES
from lofter.errors import ConfigError

from .theme import palette_of

__all__ = ["ScrollFrame", "FormPanel", "LogPanel", "AccountDialog", "center_window",
           "DEFAULT_PALETTE"]

DEFAULT_PALETTE = palette_of("浅色")


def center_window(window: tk.Misc, width: int, height: int) -> None:
    """把窗口居中；超出屏幕时收进左上角可见区域。"""
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    width = min(width, max(400, screen_w - 80))
    height = min(height, max(300, screen_h - 120))
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    window.geometry("{}x{}+{}+{}".format(width, height, x, y))


class ScrollFrame(ttk.Frame):
    """竖向滚动容器。把子控件放进 ``.interior``。"""

    def __init__(self, master, palette: Optional[dict] = None, **kwargs) -> None:
        super().__init__(master, **kwargs)
        self.palette = dict(palette or DEFAULT_PALETTE)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                background=self.palette["bg"])
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        self.interior = ttk.Frame(self.canvas, padding=(14, 10))
        self._window = self.canvas.create_window((0, 0), window=self.interior, anchor="nw")

        self.interior.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Windows 上鼠标滚轮是 <MouseWheel>，用 bind_all 到 enter/leave 范围内
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def apply_palette(self, palette: dict) -> None:
        self.palette = dict(palette)
        try:
            self.canvas.configure(background=palette["bg"])
        except tk.TclError:
            pass

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass


class FormPanel(ttk.Frame):
    """按 ``ConfigBase.FIELDS`` 自动生成的表单。"""

    def __init__(self, master, config: ConfigBase, on_change: Optional[Callable] = None,
                 palette: Optional[dict] = None) -> None:
        super().__init__(master)
        self.palette = dict(palette or DEFAULT_PALETTE)
        self._on_change = on_change
        self._widgets: dict[str, tk.Widget] = {}
        self._kinds: dict[str, str] = {}
        self._hints: list[ttk.Label] = []

        self.scroller = ScrollFrame(self, palette=self.palette)
        self.scroller.pack(fill="both", expand=True)
        body = self.scroller.interior

        for group in config.groups():
            box = ttk.LabelFrame(body, text=group, padding=(12, 8))
            box.pack(fill="x", expand=False, pady=(0, 12))
            box.columnconfigure(1, weight=1)

            for row, spec in enumerate(config.fields_in_group(group)):
                self._build_field(box, spec, row)

        self.load(config)

    # ------------------------------------------------------------- 主题
    def apply_palette(self, palette: dict) -> None:
        self.palette = dict(palette)
        self.scroller.apply_palette(palette)
        for hint in self._hints:
            try:
                hint.configure(foreground=palette["muted"])
            except tk.TclError:
                pass
        for name, widget in self._widgets.items():
            if self._kinds[name] in ("list", "json", "text"):
                try:
                    widget.configure(background=palette["panel"],
                                     foreground=palette["text"],
                                     insertbackground=palette["text"],
                                     highlightbackground=palette["border"])
                except tk.TclError:
                    pass

    # ------------------------------------------------------------- 构建
    def _build_field(self, box: ttk.LabelFrame, spec: Field, row: int) -> None:
        label = ttk.Label(box, text=spec.label, anchor="w")
        label.grid(row=row * 2, column=0, sticky="nw", padx=(0, 10), pady=(6, 0))

        widget = self._make_widget(box, spec)
        widget.grid(row=row * 2, column=1, sticky="ew", pady=(6, 0))
        self._widgets[spec.name] = widget
        self._kinds[spec.name] = spec.kind

        if spec.help:
            hint = ttk.Label(box, text=spec.help, foreground=self.palette["muted"],
                             wraplength=560, justify="left", anchor="w")
            hint.grid(row=row * 2 + 1, column=1, sticky="ew", pady=(0, 6))
            self._hints.append(hint)

    def _make_widget(self, master, spec: Field) -> tk.Widget:
        if spec.kind == "bool":
            var = tk.BooleanVar()
            widget = ttk.Checkbutton(master, variable=var, text="启用")
            widget._var = var  # type: ignore[attr-defined]
            return widget

        if spec.kind == "choice":
            widget = ttk.Combobox(master, values=list(spec.choices), state="readonly")
            widget.bind("<<ComboboxSelected>>", lambda _e: self._changed())
            return widget

        if spec.kind in ("list", "json", "text"):
            height = 3 if spec.kind == "list" else 4
            widget = tk.Text(master, height=height, wrap="word", relief="solid",
                             borderwidth=1, highlightthickness=0, undo=True,
                             background=self.palette["panel"],
                             foreground=self.palette["text"],
                             insertbackground=self.palette["text"])
            widget.configure(font=("Consolas", 9))
            return widget

        if spec.kind == "int":
            widget = ttk.Entry(master)
            widget.configure(validate="key",
                             validatecommand=(master.register(_only_int), "%P"))
            return widget

        if spec.kind == "path":
            holder = ttk.Frame(master)
            entry = ttk.Entry(holder)
            entry.pack(side="left", fill="x", expand=True)

            def browse() -> None:
                chosen = filedialog.askdirectory(title="选择输出 / 进度目录")
                if chosen:
                    entry.delete(0, "end")
                    entry.insert(0, chosen)

            ttk.Button(holder, text="浏览…", width=8, command=browse).pack(side="left", padx=(6, 0))
            holder._entry = entry  # type: ignore[attr-defined]
            return holder

        widget = ttk.Entry(master, show="*" if spec.kind == "secret" else "")
        return widget

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    # ------------------------------------------------------------- 读写
    def load(self, config: ConfigBase) -> None:
        for name, widget in self._widgets.items():
            kind = self._kinds[name]
            value = config.get(name)
            if kind == "bool":
                widget._var.set(bool(value))  # type: ignore[attr-defined]
            elif kind == "choice":
                widget.set(str(value))  # type: ignore[attr-defined]
            elif kind in ("list", "json", "text"):
                widget.delete("1.0", "end")  # type: ignore[attr-defined]
                if kind == "list":
                    widget.insert("1.0", "\n".join(str(x) for x in (value or [])))  # type: ignore[attr-defined]
                elif kind == "json":
                    import json

                    widget.insert("1.0", json.dumps(value or {}, ensure_ascii=False, indent=2))  # type: ignore[attr-defined]
                else:
                    widget.insert("1.0", str(value or ""))  # type: ignore[attr-defined]
            elif kind == "path":
                entry = widget._entry  # type: ignore[attr-defined]
                entry.delete(0, "end")
                entry.insert(0, str(value or ""))
            else:
                widget.delete(0, "end")  # type: ignore[attr-defined]
                widget.insert(0, str(value or ""))  # type: ignore[attr-defined]

    def _raw(self, name: str):
        kind = self._kinds[name]
        widget = self._widgets[name]
        if kind == "bool":
            return widget._var.get()  # type: ignore[attr-defined]
        if kind == "choice":
            return widget.get()  # type: ignore[attr-defined]
        if kind in ("list", "json", "text"):
            return widget.get("1.0", "end").strip()  # type: ignore[attr-defined]
        if kind == "path":
            return widget._entry.get()  # type: ignore[attr-defined]
        return widget.get()  # type: ignore[attr-defined]

    def apply(self, config: ConfigBase) -> None:
        """把界面上的值写进配置对象；非法输入抛 ``ConfigError``。"""
        for name in self._widgets:
            spec = config.field(name)
            if spec is None:
                continue
            config.set(name, self._raw(name))


def _only_int(proposed: str) -> bool:
    if proposed in ("", "-"):
        return True
    try:
        int(proposed)
        return True
    except ValueError:
        return False


class LogPanel(ttk.Frame):
    """日志面板：折叠 / 清空 / 自动滚动 / 导出。

    这几项是照着另一个项目（wallpaper）的 ``LogPanel`` 做的——那边只有
    「折叠 + 清空」，这里补上自动滚动开关与导出，因为抓取任务动辄跑几十分钟，
    用户需要把日志留下来贴 issue。
    """

    def __init__(self, master, palette: Optional[dict] = None,
                 title: str = "运行日志") -> None:
        super().__init__(master)
        self.palette = dict(palette or DEFAULT_PALETTE)
        self._expanded = True
        self._autoscroll = tk.BooleanVar(value=True)

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 4))
        self._toggle_btn = ttk.Button(header, text="▾ {}".format(title), width=14,
                                      command=self.toggle)
        self._toggle_btn.pack(side="left")
        ttk.Checkbutton(header, text="自动滚动", variable=self._autoscroll).pack(side="left",
                                                                           padx=(10, 0))
        ttk.Button(header, text="导出日志", width=10, command=self.export).pack(side="right")
        ttk.Button(header, text="清空", width=6, command=self.clear).pack(side="right",
                                                                       padx=(0, 6))

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        self.text = tk.Text(self.body, wrap="word", height=18, relief="solid", borderwidth=1,
                            highlightthickness=0, background=self.palette["log_bg"],
                            foreground=self.palette["log_info"],
                            selectbackground=self.palette["log_select"],
                            font=("Consolas", 9))
        self.scroll = ttk.Scrollbar(self.body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=self.scroll.set, state="disabled")
        self.text.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        self._apply_tags()
        self._last_message = ""

    # ------------------------------------------------------------- 主题
    def apply_palette(self, palette: dict) -> None:
        self.palette = dict(palette)
        try:
            self.text.configure(background=palette["log_bg"], foreground=palette["log_info"],
                                selectbackground=palette["log_select"],
                                insertbackground=palette["text"])
        except tk.TclError:
            pass
        self._apply_tags()

    def _apply_tags(self) -> None:
        colors = {10: self.palette["log_debug"], 20: self.palette["log_info"],
                  30: self.palette["log_warn"], 40: self.palette["log_error"]}
        for level, color in colors.items():
            try:
                self.text.tag_configure("lv{}".format(level), foreground=color)
            except tk.TclError:
                pass

    # ------------------------------------------------------------- 行为
    @property
    def autoscroll(self) -> bool:
        return bool(self._autoscroll.get())

    @property
    def last_message(self) -> str:
        return self._last_message

    def toggle(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self.body.pack(fill="both", expand=True)
            self._toggle_btn.configure(text="▾ 运行日志")
        else:
            self.body.pack_forget()
            self._toggle_btn.configure(text="▸ 运行日志")

    def append(self, message: str, level: int = 20) -> None:
        text = str(message)
        self._last_message = text.strip().splitlines()[-1] if text.strip() else ""
        try:
            self.text.configure(state="normal")
            self.text.insert("end", text + "\n", "lv{}".format(level))
            if self.autoscroll:
                self.text.see("end")
            self.text.configure(state="disabled")
        except tk.TclError:
            pass

    def clear(self) -> None:
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except tk.TclError:
            pass
        self._last_message = ""

    def content(self) -> str:
        try:
            return self.text.get("1.0", "end")
        except tk.TclError:
            return ""

    def export(self) -> Optional[str]:
        """把日志另存为文本文件，返回保存路径（取消则返回 None）。"""
        from datetime import datetime

        default = "lofterSpider-日志-{}.txt".format(datetime.now().strftime("%Y%m%d-%H%M%S"))
        path = filedialog.asksaveasfilename(title="导出日志", initialfile=default,
                                           defaultextension=".txt",
                                           filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")])
        if not path:
            return None
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(self.content())
        return path


class AccountDialog(tk.Toplevel):
    """登录信息填写窗口。"""

    def __init__(self, master, account: Account, palette: Optional[dict] = None) -> None:
        super().__init__(master)
        self.palette = dict(palette or DEFAULT_PALETTE)
        self.title("登录信息")
        self.transient(master)
        self.resizable(False, False)
        self.result: Optional[Account] = None
        self._account = account

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="登录方式（cookie 名）").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.key_box = ttk.Combobox(frame, values=list(LOGIN_KEY_CHOICES), width=34)
        self.key_box.set(account.login_key)
        self.key_box.grid(row=0, column=1, sticky="ew", pady=(0, 4))

        ttk.Label(frame, text="login_auth（cookie 值）").grid(row=1, column=0, sticky="w", pady=(8, 4))
        self.auth_entry = ttk.Entry(frame, width=34, show="*")
        self.auth_entry.insert(0, account.login_auth)
        self.auth_entry.grid(row=1, column=1, sticky="ew", pady=(8, 4))

        self.show_var = tk.BooleanVar(value=False)

        def toggle_show() -> None:
            self.auth_entry.configure(show="" if self.show_var.get() else "*")

        ttk.Checkbutton(frame, text="显示明文", variable=self.show_var,
                        command=toggle_show).grid(row=2, column=1, sticky="w")

        hint = ("获取方式：浏览器打开 lofter 并登录 → F12 → Application → Cookies → "
                "https://www.lofter.com → 找到上面这个 cookie 名，复制它的值。\n"
                "首次运行会自动尝试从旧的 login_info.py 导入。")
        ttk.Label(frame, text=hint, foreground=self.palette["muted"], wraplength=420,
                  justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 12))

        # 从浏览器读取（可选依赖：没装 browser_cookie3 就把按钮置灰）
        browser_row = ttk.Frame(frame)
        browser_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        # 局部 import：这个功能是可选的，不该在模块加载时就依赖它
        from lofter import browser_cookie

        self.browser_box = ttk.Combobox(
            browser_row, state="readonly", width=18,
            values=[browser_cookie.BROWSER_LABELS[name] for name in browser_cookie.BROWSER_CHOICES])
        self.browser_box.set(browser_cookie.BROWSER_LABELS["default"])
        self.browser_box.pack(side="left")
        self.read_btn = ttk.Button(browser_row, text="从浏览器读取",
                                   command=self._read_from_browser)
        self.read_btn.pack(side="left", padx=(6, 0))
        usable, _reason = browser_cookie.available()
        if usable:
            note = "只读取本机浏览器里的这一条 cookie，不会外传"
        else:
            self.read_btn.configure(state="disabled")
            note = "要启用请先执行：{}".format(browser_cookie.PIP_HINT)
        ttk.Label(browser_row, text=note, foreground=self.palette["muted"]).pack(
            side="left", padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", command=self._save).pack(side="right")

        center_window(self, 560, 340)
        self.grab_set()
        self.auth_entry.focus_set()

    def _read_from_browser(self) -> None:
        """从浏览器读取 login_auth 并填进输入框（用户还要点「保存」才生效）。"""
        from lofter import browser_cookie

        label = self.browser_box.get()
        name = next((key for key, text in browser_cookie.BROWSER_LABELS.items() if text == label),
                    "default")
        try:
            auth = browser_cookie.read_lofter_auth(name)
        except Exception as exc:  # LofterError 或环境异常，消息里已经带排查步骤
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        self.key_box.set("LOFTER-PHONE-LOGIN-AUTH")
        self.auth_entry.delete(0, "end")
        self.auth_entry.insert(0, auth)
        messagebox.showinfo("已读取", "已从浏览器读到 login_auth，点「保存」后生效。", parent=self)

    def _save(self) -> None:
        account = Account()
        account.login_key = self.key_box.get().strip() or account.login_key
        account.login_auth = self.auth_entry.get().strip()
        if not account.login_auth:
            if not messagebox.askyesno("确认", "login_auth 是空的，这样任何任务都跑不起来。仍要保存吗？"):
                return
        self.result = account
        self.destroy()
