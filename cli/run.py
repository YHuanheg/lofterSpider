"""命令行前端。

同一个 ``FIELDS`` 声明既驱动 GUI 表单，也驱动这里的 ``--set`` 参数，所以
CLI 不需要为每个任务单独写解析逻辑。

用法举例::

    python main.py --list
    python main.py --show-config like_share_tag
    python main.py --login
    python main.py --task like_share_tag --set mode=tag --set url=https://www.lofter.com/tag/xxx/total
    python main.py --task author_img --set author_url=https://xxx.lofter.com/ --set target_tags=漫画,插画
    python main.py --task blogs --set kind=txt --set source=custom --set urls=https://a.lofter.com/post/1,https://b.lofter.com/post/2
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Optional

from lofter import __version__
from lofter.config import (CONFIG_CLASSES, LOGIN_KEY_CHOICES, PROJECT_ROOT, load_account,
                           load_task_configs, save_account, save_task_configs)
from lofter.errors import LofterError, TaskCancelled
from lofter.reporter import CliReporter
from lofter.tasks import TASKS, TASK_BY_ID, get_task, load_config, run_task

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lofterSpider",
        description="lofter 抓取工具（重构版 v{}）——不带参数运行会打开图形界面".format(__version__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="提示：所有任务参数都可以用 --set 键=值 覆盖，键名见 --show-config。",
    )
    parser.add_argument("--version", action="version", version="lofterSpider {}".format(__version__))
    parser.add_argument("--cli", action="store_true", help="强制走命令行（默认无参数时开 GUI）")
    parser.add_argument("--list", action="store_true", help="列出所有任务")
    parser.add_argument("--task", metavar="ID", help="要运行的任务 id")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="覆盖配置项，可重复；列表用英文逗号分隔")
    parser.add_argument("--base-dir", help="覆盖输出 / 进度目录")
    parser.add_argument("--show-config", nargs="?", const="__all__", metavar="ID",
                        help="打印某个任务（或全部任务）的当前配置，不执行")
    parser.add_argument("--login", action="store_true", help="交互式填写并保存登录信息")
    parser.add_argument("--login-key", choices=LOGIN_KEY_CHOICES, help="配合 --login-auth 直接写入登录信息")
    parser.add_argument("--login-auth", help="配合 --login-key 直接写入登录信息")
    parser.add_argument("--login-from-browser", action="store_true",
                        help="从本机浏览器读取 login_auth（需 pip install browser-cookie3）")
    parser.add_argument("--browser", default="default",
                        help="配合 --login-from-browser：chrome / edge / firefox / chromium / "
                             "brave / vivaldi / opera / safari / default")
    parser.add_argument("--reset-progress", action="store_true",
                        help="运行前清空该任务的进度文件（会重新全量抓取）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    parser.add_argument("--no-input", action="store_true",
                        help="无人值守模式：需要确认的地方一律用默认值，不阻塞")
    return parser


def _print_task_list() -> None:
    print("可用任务（{}）\n".format(len(TASKS)))
    width = max(len(task.tid) for task in TASKS)
    for task in TASKS:
        print("  {:<{w}}  {}".format(task.tid, task.name, w=width))
        if task.summary:
            print("  {:<{w}}    {}".format("", task.summary, w=width))
    print("\n用 --show-config <ID> 查看某个任务的参数；用 --set 键=值 覆盖参数。")


def _print_config(tid: str, configs: dict) -> None:
    cfg = load_config(tid, configs)
    task = get_task(tid)
    print("# {} ({})\n# {}".format(task.name, tid, task.summary))
    for group in cfg.groups():
        print("\n[{}]".format(group))
        for spec in cfg.fields_in_group(group):
            value = cfg.get(spec.name)
            if spec.kind == "secret":
                value = "***已设置***" if value else "(空)"
            print("  {:<20} = {!r}".format(spec.name, value))
            if spec.help:
                print("  {:<20}   {}".format("", spec.help))
    print()


def _parse_overrides(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise LofterError("--set 需要 KEY=VALUE 形式，收到 {!r}".format(pair))
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _apply_overrides(cfg, overrides: dict, tid: str) -> None:
    from lofter.config import ConfigError

    for key, value in overrides.items():
        spec = cfg.field(key)
        if spec is None:
            raise ConfigError("任务 {} 没有配置项 {}，可用项：{}".format(
                tid, key, ", ".join(f.name for f in cfg.FIELDS)))
        if spec.kind == "list" and "," in value:
            value = [x.strip() for x in value.split(",") if x.strip()]
        cfg.set(key, value)


def _do_login(args: argparse.Namespace) -> int:
    account = load_account()
    if getattr(args, "login_from_browser", False):
        # 可选能力：browser-cookie3 没装时 read_lofter_auth 会给出安装命令
        from lofter import browser_cookie

        try:
            auth = browser_cookie.read_lofter_auth(getattr(args, "browser", "default"))
        except LofterError as exc:
            print("\n[读取失败] {}".format(exc), file=sys.stderr)
            return 1
        account.login_key = "LOFTER-PHONE-LOGIN-AUTH"
        account.login_auth = auth
        print("已从浏览器读取到 login_auth（{} 个字符）".format(len(auth)))
    elif args.login_key and args.login_auth:
        account.login_key = args.login_key
        account.login_auth = args.login_auth
    else:
        print("登录方式（cookie 名）可选：{}".format(" / ".join(LOGIN_KEY_CHOICES)))
        key = input("login_key [{}]: ".format(account.login_key)).strip() or account.login_key
        auth = getpass.getpass("login_auth（输入时不显示）: ").strip() or account.login_auth
        account.login_key = key
        account.login_auth = auth
    save_account(account)
    print("已保存到 config/account.json（login_auth {}）".format(
        "已设置" if account.ready else "仍为空"))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.login or getattr(args, "login_from_browser", False):
        return _do_login(args)

    if args.list:
        _print_task_list()
        return 0

    configs = load_task_configs()

    if args.show_config:
        if args.show_config == "__all__":
            for task in TASKS:
                _print_config(task.tid, configs)
        else:
            if args.show_config not in TASK_BY_ID:
                parser.error("未知任务 {}".format(args.show_config))
            _print_config(args.show_config, configs)
        return 0

    if not args.task:
        parser.print_help()
        return 2

    if args.task not in TASK_BY_ID:
        parser.error("未知任务 {}，可选：{}".format(args.task, " / ".join(TASK_BY_ID)))

    reporter = CliReporter(verbose=args.verbose)
    if args.no_input:
        reporter.interactive = False

    try:
        cfg = load_config(args.task, configs)
        if args.overrides:
            _apply_overrides(cfg, _parse_overrides(args.overrides), args.task)
        if args.base_dir:
            cfg.base_dir = args.base_dir
        if args.reset_progress:
            task = get_task(args.task)
            from lofter.progress import StageFiles

            removed = StageFiles(task.base_dir_of(cfg)).reset_all_files()
            print("已清理进度文件：{}".format(removed or "无"))

        # 参数解析成功后立刻落盘，方便下次少打一长串 --set
        configs[args.task] = cfg
        save_task_configs(configs)

        account = load_account()
        t0 = time.time()
        run_task(args.task, cfg, account=account, reporter=reporter)
        print("\n完成，耗时 {:.1f} 秒".format(time.time() - t0))
        return 0
    except TaskCancelled:
        print("\n已取消")
        return 130
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）")
        return 130
    except LofterError as exc:
        print("\n[错误] {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
