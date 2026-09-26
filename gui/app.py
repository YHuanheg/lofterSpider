"""主窗口。

交互流程
--------
1. 首次运行：点「登录信息」填 ``login_auth``（会自动尝试从旧的 ``login_info.py`` 导入）。
2. 左侧选任务 → 右侧按该任务的 ``FIELDS`` 自动生成参数表单，填完点「保存配置」。
3. 点「开始」→ 任务在后台线程跑，界面自动切到「运行日志」，进度条与阶段实时更新；
   任务中途需要你确认时会弹窗，点「取消」可随时中断。
4. 跑完看底部「统计」一行，点「打开输出文件夹」看结果；断点续跑直接再点「开始」即可。

界面结构参考了另一个项目（wallpaper）的 GUI：

* 顶部工具栏 = 标题 + 版本 + **主题切换**（跟随系统 / 浅色 / 深色）；
* 底部状态栏 = 输出目录摘要 + 进度 + 状态 + **最近一条日志**；
* 任务结束后用**统计卡片**展示产出（对应它那边 ``run_*() -> Stats`` 的用法）；
* 日志面板支持**折叠 / 清空 / 自动滚动 / 导出**；
* 关闭时统一走一遍资源释放循环。

但底层仍然完全沿用本项目既有架构：参数表单由 ``Field`` 声明生成、日志走
``Reporter`` 接口、任务在后台线程 + 队列回主线程。没有引入 customtkinter
一类的新依赖——``ttk`` + 自维护调色板就够，避免给用户增加安装负担。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from lofter import __version__
from lofter.config import (CONFIG_DIR, Account, load_account, load_app_state,
                           load_task_configs, save_account, save_app_state,
                           save_task_configs)
from lofter.errors import ConfigError, LofterError
from lofter.tasks import TASKS, TASK_BY_ID, TaskResult, get_task

from .theme import THEME_VALUES, ThemeManager
from .widgets import AccountDialog, FormPanel, LogPanel, center_window
from .worker import BackgroundRunner

__all__ = ["App", "main"]

WINDOW_W, WINDOW_H = 1020, 740
GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)?$")


def open_path(path: str) -> None:
    """用系统默认方式打开文件/目录（不存在就先建出来，避免打开报错）。"""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # pragma: no cover - 平台相关
        raise LofterError("打不开 {}：{}".format(path, exc)) from exc


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("lofterSpider {} — lofter 抓取工具".format(__version__))
        self.minsize(900, 620)

        self.account: Account = load_account()
        self.configs: dict = load_task_configs()
        self.state_: dict = load_app_state()
        self.runner = BackgroundRunner()

        self.current_tid = ""
        self.form: FormPanel | None = None
        self._progress_indeterminate = False
        self._closing = False
        self._theme: ThemeManager | None = None
        self._components: list = []

        self._build_menu()
        self._build_layout()

        # 主题要在控件建好之后应用，之后任何切换都走 _change_theme
        self._theme = ThemeManager(self, str(self.state_.get("theme") or "跟随系统"))
        self._theme.watch(self._on_palette)
        self._apply_saved_geometry()
        # 「跟随系统」时在窗口获得焦点时复查一次，用户在系统里改了主题不用重启
        self.bind("<FocusIn>", lambda _e: self._theme and self._theme.refresh())

        last = self.state_.get("last_task") or TASKS[0].tid
        self.select_task(last if last in TASK_BY_ID else TASKS[0].tid)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self._pump)

    # ---------------------------------------------------------------- 菜单
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开输出文件夹", command=self.open_output_dir)
        file_menu.add_command(label="打开配置文件夹（config/）",
                              command=lambda: self._safe_open(str(CONFIG_DIR)))
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="导出当前日志", command=lambda: self.log.export())
        help_menu.add_command(label="关于", command=self._about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.configure(menu=menubar)

    # ---------------------------------------------------------------- 界面
    def _build_layout(self) -> None:
        header = ttk.Frame(self, padding=(16, 12, 16, 6))
        header.pack(fill="x")
        ttk.Label(header, text="lofterSpider",
                  font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Label(header, text="  v{}".format(__version__),
                  foreground="#8b95a1").pack(side="left", padx=(6, 0))

        self.account_label = ttk.Label(header, text="")
        self.account_label.pack(side="right", padx=(8, 0))
        ttk.Button(header, text="登录信息", command=self.edit_account).pack(side="right")

        ttk.Label(header, text="主题:").pack(side="right", padx=(16, 4))
        self.theme_box = ttk.Combobox(header, values=list(THEME_VALUES), state="readonly",
                                      width=10)
        self.theme_box.set(str(self.state_.get("theme") or "跟随系统"))
        self.theme_box.bind("<<ComboboxSelected>>",
                            lambda _e: self._change_theme(self.theme_box.get()))
        self.theme_box.pack(side="right")

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        left = ttk.Frame(paned, padding=(0, 0, 10, 0))
        paned.add(left, weight=0)
        ttk.Label(left, text="选择任务",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.task_list = tk.Listbox(left, width=30, activestyle="none", exportselection=False,
                                    font=("Microsoft YaHei UI", 10), borderwidth=1,
                                    relief="solid", highlightthickness=1)
        self.task_list.pack(fill="y", expand=False, pady=(6, 8))
        self.task_list.bind("<<ListboxSelect>>", self._on_task_pick)
        for task in TASKS:
            self.task_list.insert("end", "  {}".format(task.name))

        self.task_summary = ttk.Label(left, text="", wraplength=220, justify="left")
        self.task_summary.pack(anchor="w")

        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)

        self.form_host = ttk.Frame(self.notebook)
        self.notebook.add(self.form_host, text="参数设置")

        log_host = ttk.Frame(self.notebook, padding=(10, 8))
        self.notebook.add(log_host, text="运行日志")
        self.log = LogPanel(log_host)
        self.log.pack(fill="both", expand=True)
        self._components.append(self.log)

        footer = ttk.Frame(self, padding=(16, 0, 16, 10))
        footer.pack(fill="x")

        info_row = ttk.Frame(footer)
        info_row.pack(fill="x", pady=(0, 6))
        self.dir_label = ttk.Label(info_row, text="输出目录：-")
        self.dir_label.pack(side="left")
        ttk.Button(info_row, text="打开输出文件夹",
                   command=self.open_output_dir).pack(side="right")

        prog_row = ttk.Frame(footer)
        prog_row.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(prog_row, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status = ttk.Label(prog_row, text="就绪", width=32, anchor="e")
        self.status.pack(side="right", padx=(10, 0))

        stats_row = ttk.Frame(footer)
        stats_row.pack(fill="x", pady=(0, 6))
        ttk.Label(stats_row, text="统计：").pack(side="left")
        self.stats_label = ttk.Label(stats_row, text="尚无（跑完一个任务会显示产出）")
        self.stats_label.pack(side="left", fill="x", expand=True)

        btn_row = ttk.Frame(footer)
        btn_row.pack(fill="x")
        self.save_btn = ttk.Button(btn_row, text="保存配置", command=self.save_current_config)
        self.save_btn.pack(side="left")
        self.start_btn = ttk.Button(btn_row, text="开始", command=self.start_task)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(btn_row, text="取消", command=self.cancel_task,
                                    state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 8))

        self.last_log_label = ttk.Label(footer, text="", anchor="w")
        self.last_log_label.pack(fill="x", pady=(6, 0))

        self._refresh_account_label()

    # ---------------------------------------------------------------- 主题
    def _on_palette(self, palette: dict) -> None:
        """主题管理器通知：把所有不吃 ttk style 的控件重绘一遍。"""
        for widget in (self.task_list,):
            try:
                widget.configure(background=palette["list_bg"], foreground=palette["text"],
                                 selectbackground=palette["list_select_bg"],
                                 selectforeground=palette["list_select_fg"],
                                 highlightbackground=palette["border"],
                                 highlightcolor=palette["accent"])
            except tk.TclError:
                pass
        try:
            self.account_label.configure(
                foreground="#2f855a" if self.account.ready else palette["log_error"])
            for label in (self.task_summary, self.dir_label, self.status,
                          self.last_log_label, self.stats_label):
                label.configure(foreground=palette["muted"])
        except tk.TclError:
            pass
        if self.form is not None:
            self.form.apply_palette(palette)
        self.log.apply_palette(palette)

    def _change_theme(self, value: str) -> None:
        if self._theme is None:
            return
        mode = self._theme.set_mode(value)
        self.theme_box.set(mode)
        self.state_["theme"] = mode
        save_app_state(self.state_)
        self.log.append("已切换主题：{}".format(mode), 20)

    # ------------------------------------------------------------ 窗口几何
    def _apply_saved_geometry(self) -> None:
        """应用保存的几何，并**收进当前屏幕可见区域**。

        配置可能被手改坏、或窗口上次被拖到已断开的副屏，直接套用会导致
        窗口跑到看不见的地方。这里解析不出来就居中，越界就夹回来。
        """
        raw = str(self.state_.get("geometry") or "")
        match = GEOMETRY_RE.match(raw)
        if not match:
            center_window(self, WINDOW_W, WINDOW_H)
            return
        width, height = int(match.group(1)), int(match.group(2))
        x = int(match.group(3))
        y = int(match.group(4) or 0)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = max(600, min(width, screen_w - 40))
        height = max(400, min(height, screen_h - 80))
        x = max(0, min(x, max(0, screen_w - width)))
        y = max(0, min(y, max(0, screen_h - height)))
        try:
            self.geometry("{}x{}+{}+{}".format(width, height, x, y))
        except tk.TclError:
            center_window(self, WINDOW_W, WINDOW_H)

    # ------------------------------------------------------------ 任务切换
    def _on_task_pick(self, _event=None) -> None:
        selection = self.task_list.curselection()
        if not selection:
            return
        self.select_task(TASKS[selection[0]].tid)

    def select_task(self, tid: str) -> None:
        if self.runner.busy:
            messagebox.showinfo("正在运行", "任务运行中不能切换任务，请先等它结束或点「取消」。")
            self._sync_list_selection()
            return
        if self.current_tid and self.form is not None:
            self._stash_form(self.current_tid)

        self.current_tid = tid
        task = get_task(tid)
        self.task_summary.configure(text=task.summary)

        for child in self.form_host.winfo_children():
            child.destroy()
        cfg = self.configs.get(tid) or task.config_class()
        palette = self._theme.palette if self._theme else None
        self.form = FormPanel(self.form_host, cfg, on_change=self._refresh_dir_label,
                              palette=palette)
        self.form.pack(fill="both", expand=True)

        self._sync_list_selection()
        self._refresh_dir_label()

    def _sync_list_selection(self) -> None:
        index = [t.tid for t in TASKS].index(self.current_tid)
        self.task_list.selection_clear(0, "end")
        self.task_list.selection_set(index)
        self.task_list.see(index)

    def _stash_form(self, tid: str) -> None:
        """把界面上的值收回配置对象；非法输入就沿用上一次的合法值。"""
        if self.form is None or tid not in self.configs:
            return
        try:
            self.form.apply(self.configs[tid])
        except ConfigError:
            pass

    # ------------------------------------------------------------ 配置动作
    def save_current_config(self) -> None:
        if self.form is None:
            return
        cfg = self.configs[self.current_tid]
        try:
            self.form.apply(cfg)
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc))
            return
        save_task_configs(self.configs)
        self._refresh_dir_label()
        self.status.configure(text="配置已保存")
        messagebox.showinfo("已保存", "配置已写入 config/settings.json")

    def _refresh_dir_label(self) -> None:
        try:
            cfg = self.configs[self.current_tid]
            if self.form is not None:
                self.form.apply(cfg)
            base = get_task(self.current_tid).base_dir_of(cfg)
            self.configs[self.current_tid] = cfg
        except (ConfigError, LofterError) as exc:
            self.dir_label.configure(text="输出目录：（配置还不完整：{}）".format(exc))
            return
        self.dir_label.configure(text="输出目录：{}".format(base))

    def _refresh_account_label(self) -> None:
        palette = self._theme.palette if self._theme else None
        if self.account.ready:
            self.account_label.configure(text="已登录（{}）".format(self.account.login_key),
                                         foreground="#2f855a")
        else:
            color = palette["log_error"] if palette else "#c53030"
            self.account_label.configure(text="未填写登录信息", foreground=color)

    def edit_account(self) -> None:
        palette = self._theme.palette if self._theme else None
        dialog = AccountDialog(self, self.account, palette=palette)
        self.wait_window(dialog)
        if dialog.result is not None:
            self.account = dialog.result
            save_account(self.account)
            self._refresh_account_label()
            self.log.append("登录信息已保存（{}）".format(self.account.login_key))

    def open_output_dir(self) -> None:
        try:
            cfg = self.configs.get(self.current_tid)
            path = get_task(self.current_tid).base_dir_of(cfg)
            self._safe_open(path)
        except LofterError as exc:
            messagebox.showerror("打不开", str(exc))

    def _safe_open(self, path: str) -> None:
        try:
            open_path(path)
        except LofterError as exc:
            messagebox.showerror("打不开", str(exc))

    # ---------------------------------------------------------------- 运行
    def start_task(self) -> None:
        if self.runner.busy:
            return
        cfg = self.configs[self.current_tid]
        try:
            if self.form is not None:
                self.form.apply(cfg)
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc))
            return

        task = get_task(self.current_tid)
        try:
            task.validate(cfg)
        except ConfigError as exc:
            messagebox.showerror("配置有误", str(exc))
            return

        if not self.account.ready:
            messagebox.showwarning("缺少登录信息", "还没有填 login_auth，先点右上角「登录信息」。")
            self.edit_account()
            if not self.account.ready:
                return

        save_task_configs(self.configs)
        self.log.clear()
        self.notebook.select(1)
        self.log.append("=== 开始：{} ===".format(task.name), 20)
        self.stats_label.configure(text="运行中…")
        self.last_log_label.configure(text="")

        self.progress.configure(mode="determinate", value=0)
        self._set_running(True)
        try:
            self.runner.start(self.current_tid, cfg, self.account)
        except Exception as exc:
            self._set_running(False)
            messagebox.showerror("启动失败", str(exc))

    def cancel_task(self) -> None:
        if not self.runner.busy:
            return
        self.runner.cancel()
        self.status.configure(text="正在取消…")
        self.log.append("已请求取消，等待当前步骤结束…", 30)

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        self.save_btn.configure(state="disabled" if running else "normal")
        self.task_list.configure(state="disabled" if running else "normal")
        if not running:
            self.status.configure(text="就绪")

    # ------------------------------------------------------------ 事件循环
    def _pump(self) -> None:
        try:
            while True:
                self._handle_event(self.runner.events.get_nowait())
        except Exception:
            pass
        finally:
            if not self._closing:
                self._tick_status()
                self.after(80, self._pump)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            _, level, msg = event
            self.log.append(str(msg), level)
            self.last_log_label.configure(text="最近：{}".format(self.log.last_message))
        elif kind == "stage":
            self.log.append("【{}】".format(event[1]), 30)
            self.status.configure(text=str(event[1])[:30])
        elif kind == "progress":
            _, current, total, desc = event
            if total:
                if self._progress_indeterminate:
                    self.progress.stop()
                    self._progress_indeterminate = False
                    self.progress.configure(mode="determinate")
                self.progress.configure(maximum=total, value=current)
                self.status.configure(text="{} {} / {}".format(desc or "进度", current, total))
            else:
                if not self._progress_indeterminate:
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(60)
                    self._progress_indeterminate = True
                self.status.configure(text=desc or "进行中…")
        elif kind == "ask":
            _, question, default, reply = event
            self._answer(reply, self._ask_dialog(question, default))
        elif kind == "confirm":
            _, question, default, reply = event
            self._answer(reply, messagebox.askyesno(
                "需要确认", question, default="yes" if default else "no"))
        elif kind == "done":
            _, ok, message = event[0], event[1], event[2]
            result = event[3] if len(event) > 3 else None
            self._finish(ok, message, result)

    def _ask_dialog(self, question: str, default) -> str:
        from tkinter import simpledialog

        answer = simpledialog.askstring("需要输入", question,
                                        initialvalue=str(default or ""), parent=self)
        return "" if answer is None else answer

    @staticmethod
    def _answer(reply, value) -> None:
        try:
            reply.put_nowait(value)
        except Exception:
            pass

    def _render_stats(self, result: TaskResult) -> None:
        """把 TaskResult 渲染成统计卡片文字（对应 wallpaper 的 Stats 展示）。"""
        chunks = []
        for key, value in result.stats.items():
            chunks.append("{} {}".format(key, value))
        if result.elapsed:
            chunks.append("耗时 {:.1f}s".format(result.elapsed))
        self.stats_label.configure(text=" | ".join(chunks) if chunks else "无统计数据")
        if result.output_dir:
            self.dir_label.configure(text="输出目录：{}".format(result.output_dir))

    def _finish(self, ok: bool, message: str, result: TaskResult | None = None) -> None:
        self._set_running(False)
        if self._progress_indeterminate:
            self.progress.stop()
            self._progress_indeterminate = False
            self.progress.configure(mode="determinate")
        if ok:
            self.log.append("=== 完成 ===", 20)
            self.status.configure(text="完成")
            if result is not None:
                self.log.append(result.summary(), 20)
                self._render_stats(result)
        else:
            self.log.append("=== 结束：{} ===".format(message), 40)
            self.status.configure(text=str(message)[:30])
            if result is not None:
                self._render_stats(result)
            if message != "已取消":
                messagebox.showerror("任务失败", str(message))

    def _tick_status(self) -> None:
        if self.runner.busy:
            self.title("lofterSpider {} — 运行中 {:.0f}s".format(__version__,
                                                            self.runner.elapsed))
        else:
            self.title("lofterSpider {} — lofter 抓取工具".format(__version__))

    # -------------------------------------------------------------- 收尾
    def _about(self) -> None:
        messagebox.showinfo(
            "关于",
            "lofterSpider {}\n\n"
            "抓取 lofter 的合集 / 喜欢 / 推荐 / tag / 作者主页内容。\n"
            "架构：核心包（lofter/）+ 命令行（cli/）+ 图形界面（gui/），"
            "适配 Python 3.12。\n\n"
            "界面风格参考了 wallpaper 项目的 GUI 结构。\n\n"
            "配置文件：config/settings.json（不含凭证）\n"
            "凭证文件：config/account.json".format(__version__))

    def _release_resources(self) -> None:
        """统一释放组件资源（对应 wallpaper 关闭时的 release 循环）。

        目前只有日志面板，但留成循环，将来加托盘 / 文件监视 / 定时器时
        不会漏掉某处清理。
        """
        for component in list(self._components):
            for name in ("cleanup", "detach_log_handlers", "on_closing"):
                method = getattr(component, name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass

    def on_close(self) -> None:
        if self.runner.busy:
            if not messagebox.askyesno("任务还在运行", "任务还没结束，确定要退出吗？"):
                return
            self.runner.cancel()
        self._closing = True
        try:
            self._stash_form(self.current_tid)
            save_task_configs(self.configs)
            self.state_["geometry"] = self.geometry()
            self.state_["last_task"] = self.current_tid
            save_app_state(self.state_)
        except Exception:
            pass
        self._release_resources()
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
