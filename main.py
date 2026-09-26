#!/usr/bin/env python
"""lofterSpider 统一入口。

* **不带任何参数**（例如双击 ``run_gui.bat``）→ 打开图形界面；
* 带参数 → 走命令行，见 ``python main.py --help``。

两种方式用的是同一套核心包与同一份配置文件，界面里改过的参数命令行也能直接用。
"""

from __future__ import annotations

import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args == ["--gui"]:
        try:
            from gui import launch
        except ImportError as exc:  # pragma: no cover - 环境相关
            print("无法加载图形界面（{}），改走命令行。用 --list 查看任务。".format(exc))
            return 2
        return launch()

    from cli.run import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
