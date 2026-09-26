"""真跑一次 tkinter 主循环：开窗 → 跑 2.5 秒事件泵 → 自动关。

冒烟测试里的 GUI 用例只构建窗口、不跑 mainloop。这个脚本补上最后一段：
证明 `after()` 事件泵、窗口绘制、关闭流程在真实主循环里也没问题。

运行::

    <python> tests/gui_launch_check.py

窗口会自己弹出并在 2.5 秒后关闭，退出码 0 表示正常。
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    try:
        import tkinter as tk
    except Exception as exc:
        print("跳过：没有 tkinter（{}）".format(exc))
        return 0

    from lofter import config as cfg_mod

    from pathlib import Path
    from gui.app import App

    workdir = tempfile.mkdtemp(prefix="lofter-gui-check-")
    cfg_mod.CONFIG_DIR = Path(workdir)
    cfg_mod.SETTINGS_FILE = cfg_mod.CONFIG_DIR / "settings.json"
    cfg_mod.ACCOUNT_FILE = cfg_mod.CONFIG_DIR / "account.json"

    try:
        app = App()
    except tk.TclError as exc:
        print("跳过：无显示环境（{}）".format(exc))
        return 0

    marks: list[str] = []

    def step_done() -> None:
        marks.append("after 事件泵执行了")
        print("✓ 主循环运行中，当前任务：{}".format(app.current_tid))
        app.destroy()

    app.after(2500, step_done)
    app.mainloop()

    if marks:
        print("✓ 窗口正常打开并关闭，退出码 0")
        return 0
    print("✗ 主循环没有跑到回调")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
