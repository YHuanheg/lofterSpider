"""离线冒烟测试 —— 不需要登录、不发任何真实请求。

运行::

    <python> tests/smoke_test.py            # 全量
    <python> tests/smoke_test.py --no-gui   # 无显示环境下跳过 GUI 构建用例

覆盖范围
--------
1. 依赖与全模块导入（``-W error::SyntaxWarning`` 下不能报错）
2. 纯函数：文件名清洗 / 重名 / 图片过滤 / tag 过滤 / DWR 转义 / 时间换算
3. 正文模板匹配（7 个模板 + 通用兜底）
4. 归档页条目解析（l4 与 l9 两套正则）
5. l13 的原始数据解析（在家造一段 DWR 响应，验证字段、tag 小写化、图片链接挑选）
6. 配置读写、类型强制、FIELDS 与 dataclass 字段一致性、旧 login_info.py 迁移
7. 进度文件语义（StageFiles）
8. Reporter 协议，含 GUI 的「跨线程提问」握手
9. l13 阶段2/3/4 的离线端到端（关掉所有保存项，纯本地跑完）
10. CLI 子进程：--list / --show-config / --help
11. GUI 无 mainloop 构建（构建 → 切任务 → 建表单 → 销毁）
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 用例在模块导入时就会依次执行，所以这个开关要在最前面读
NO_GUI = "--no-gui" in sys.argv

# 依赖 sys.path 已就绪，这几行必须放在上面之后
import re  # noqa: E402

from lofter.errors import TaskCancelled  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """装饰器式用例：抛异常即失败。结果边跑边打印，方便中途被中断也能看到进度。"""
    def wrapper(func):
        try:
            detail = func()
            RESULTS.append((name, True, detail or ""))
            print("  ✓ {}".format(name), flush=True)
        except Exception as exc:  # noqa: BLE001
            import traceback

            RESULTS.append((name, False, "{}: {}".format(type(exc).__name__, exc)))
            print("  ✗ {}  → {}: {}".format(name, type(exc).__name__, exc), flush=True)
            if os.environ.get("SMOKE_TRACE"):
                traceback.print_exc()
        return func

    return wrapper


def expect(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ============================================================ 1. 导入与语法
@check("依赖可导入（requests / lxml / html2text）")
def t_deps():
    import html2text  # noqa: F401
    import lxml  # noqa: F401
    import requests  # noqa: F401

    return "requests {} / lxml {} / html2text {}".format(
        requests.__version__, lxml.__version__, getattr(html2text, "__version__", "?"))


@check("全模块导入无 SyntaxWarning（3.12 非法转义）")
def t_imports():
    import lofter
    from lofter import archive, config, errors, net, progress, reporter, templates, utils  # noqa: F401
    from lofter.tasks import TASKS  # noqa: F401

    problems = []
    checked = 0
    for folder in ("lofter", "cli", "gui"):
        for base, _dirs, files in os.walk(os.path.join(ROOT, folder)):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(base, filename)
                checked += 1
                with open(path, "r", encoding="utf-8") as fp:
                    source = fp.read()
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    compile(source, path, "exec")
                for item in caught:
                    if issubclass(item.category, SyntaxWarning):
                        problems.append("{}:{}".format(os.path.relpath(path, ROOT), item.lineno))
    expect(not problems, "仍有非法转义序列：{}".format(problems))
    return "lofter {} / 检查 {} 个文件，零语法告警".format(lofter.__version__, checked)


# ================================================================= 2. 纯函数
@check("utils.sanitize_filename 清洗 Windows 非法字符")
def t_sanitize():
    from lofter.utils import sanitize_filename

    raw = 'a/b|c\\d<e>f:g"h?i*j(k)\r\nl\tm'
    out = sanitize_filename(raw)
    for bad in '\\/:*?"<>|\r\n\t':
        expect(bad not in out, "{!r} 仍在 {!r} 里".format(bad, out))
    expect(out.startswith("a&b&c&d《e》f：g”h？i·j（k）"), "替换结果不符合预期：{!r}".format(out))
    expect(sanitize_filename("标题,带逗号", fullwidth_comma=True) == "标题，带逗号", "全角逗号开关失效")
    expect(sanitize_filename("标题,带逗号") == "标题,带逗号", "默认不该动半角逗号")
    return repr(out)


@check("utils.dedup_filename 同名去重语义")
def t_dedup():
    from lofter.utils import dedup_filename

    with tempfile.TemporaryDirectory() as tmp:
        name = "同名 by 作者.txt"
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fp:
            fp.write("内容A")
        expect(dedup_filename(name, "内容A", tmp, "txt") == name, "内容相同应返回原名")
        expect(dedup_filename(name, "内容B", tmp, "txt") == "同名 by 作者(2).txt", "内容不同应加 (2)")
        expect(dedup_filename("新文件.txt", "x", tmp, "txt") == "新文件.txt", "不存在应返回原名")
    return "同名→原名 / 异内容→(2) / 新文件→原名"


@check("utils 图片过滤与 tag 过滤")
def t_img_tag():
    from lofter.utils import detect_img_type, filter_img_urls, is_lofter_img, tag_match

    urls = [
        "https://imglf1.lf1.net/a.jpg?imageView",                    # 去掉裁剪参数后保留
        "https://imglf1.lf1.net/b.png",
        "https://imglf1.lf1.net/b.png",                              # 重复
        "https://imglf1.lf1.net/tiny.jpg?thumbnail=16y16",           # 缩略图尺寸 → 丢弃
        "https://example.com/c.gif?amp;16&amp;",                     # img 类型带 &amp; → 丢弃
        "https://imglf1.lf1.net/d.jpg",
    ]
    kept = filter_img_urls(urls, "img")
    expect(kept == ["https://imglf1.lf1.net/a.jpg?", "https://imglf1.lf1.net/b.png",
                    "https://imglf1.lf1.net/d.jpg"],
           "过滤结果异常：{}".format(kept))
    # 注意：`imageView` 的切法会产生一个尾随 `?`，这是原脚本就有的行为，HTTP 语义上等价
    # 同样的 &amp; 链接在 text/article 类型下不丢（保持原语义）
    kept_text = filter_img_urls(["https://example.com/e.jpg?amp;&amp;"], "text")
    expect(len(kept_text) == 1, "text 类型不该因为 &amp; 丢弃：{}".format(kept_text))
    expect(detect_img_type("x.png") == "png" and detect_img_type("x.gif") == "gif"
           and detect_img_type("x.jpg") == "jpg", "类型识别错误")
    expect(is_lofter_img("https://imglf1.lf1.net/a.jpg"), "站内图判定失败")
    expect(not is_lofter_img("https://example.com/a.jpg"), "外链被误判为站内图")
    expect(tag_match([], ["a"], "in") is True and tag_match([], ["a"], "out") is False,
           "空 tag 的 in/out 语义不符")
    expect(tag_match(["b"], [], "out") is True, "target 为空应直接放行")
    return "保留 {} 张（原 {}）".format(len(kept), len(urls))


@check("utils DWR 转义还原 / literal_list / 时间换算")
def t_escape():
    from lofter.utils import js_unescape_latin, js_unescape_utf8, literal_list, ts_ms_to_date

    expect(js_unescape_latin(r"\u4f5c\u8005") == "作者", "unicode_escape 还原失败")
    expect(js_unescape_latin("中文未转义") == "中文未转义", "非 ASCII 应退回原串而不是丢内容")
    expect(js_unescape_utf8(r"\u8bc4\u8bba") == "评论", "评论转义还原失败")
    raw = r'[{"raw":"https://x/a.jpg","orign":"https://x/a.jpg?imageView"}]'
    value = literal_list(raw)
    expect(isinstance(value, list) and value[0]["raw"] == "https://x/a.jpg", "literal_list 失败")
    expect(literal_list("这不是列表") == [], "非法输入应返回空列表而不是抛异常")
    expect(ts_ms_to_date(1689000000000).startswith("2023-07"), "时间戳换算错误")
    return "转义/字面量/时间均正确"


# ================================================================ 3. 模板匹配
@check("templates 模板识别与正文抽取")
def t_templates():
    from lxml.html import etree

    from lofter import templates

    html = '<html><body><div class="content"><div class="text">标题\n正文内容</div></div></body></html>'
    parse = etree.HTML(html)
    expect(templates.matcher(parse) == 1, "应命中模板1")
    content = templates.get_content(parse, 1, "标题", "article")
    expect("正文内容" in content and "标题" not in content, "模板1应删掉重复标题：{!r}".format(content))

    generic = etree.HTML("<html><body><div>随意</div><p>x</p></body></html>")
    expect(templates.matcher(generic) == 0, "无匹配时应返回通用模板 0")
    expect(templates.get_content(generic, 0, "t", "text"), "通用模板应能取到内容")
    return "模板1命中 / 兜底模板0 可用"


# ============================================================== 4. 归档页解析
@check("archive 归档条目解析（图片 / 文本两套）")
def t_archive():
    from lofter.archive import parse_archive_img_entries, parse_archive_txt_entries

    img_entry = ('s0.blogId=1;\ns0.imgurl="https://imglf1.lf1.net/x.jpg";'
                 's0.permalink="1a2b_100";s0.time=1689000000000;\nnoticeLinkTitle')
    txt_entry = ('s1.blogId=2;\ns1.permalink="1a2b_101";s1.time=1689000000000;'
                 r's1.title="\u6807\u9898\u4e00";s1.content="<p>x</p>";' + "\n")

    imgs = parse_archive_img_entries([img_entry], "https://a.lofter.com/")
    expect(len(imgs) == 1 and imgs[0]["blog_url"].endswith("/post/1a2b_100"), "图片条目解析失败")
    expect(imgs[0]["time"].startswith("2023-07"), "图片条目时间解析失败")

    txts = parse_archive_txt_entries([txt_entry], "https://a.lofter.com/", "作者")
    expect(len(txts) == 1 and txts[0]["title"] == "标题一", "文本条目标题解析失败")
    expect(txts[0]["blog_type"] == "article", "应判定为 article")
    expect(parse_archive_img_entries([img_entry], "https://a.lofter.com/",
                                     end_time="2000-01-01") == [], "结束时间过滤失效")
    return "图片/文本条目、时间过滤均正确"


# ================================================= 5. l13 原始数据解析（离线）
DWR_BLOB = (
    r's1.blogPageUrl="https://writer.lofter.com/post/1a2b_100";'
    r's1.opTime=1690000000000;'
    r's1.hot=88;'
    r's1.blogNickName="\u4f5c\u8005\u7532";'
    r's1.publishTime=1689000000000;'
    r's1.tags="\u6807\u7b7eA,\u6807\u7b7eB";'
    r's1.title="\u957f\u6587\u7ae0";'
    r's1.content="<p>\u6b63\u6587\u4e00</p>";'
    "\n\n"
    r's2.blogPageUrl="https://writer.lofter.com/post/1a2b_101";'
    r's2.opTime=1690000001000;'
    r's2.hot=1;'
    r's2.blogNickName="\u4f5c\u8005\u7532";'
    r's2.publishTime=1689000001000;'
    r's2.tags="";'
    r's2.title="";'
    r's2.content="<p>\u65e0\u6807\u9898\u6b63\u6587</p>";'
    "\n\n"
    r's3.blogPageUrl="https://writer.lofter.com/post/1a2b_102";'
    r's3.opTime=1690000002000;'
    r's3.hot=500;'
    r's3.blogNickName="\u4f5c\u8005\u4e59";'
    r's3.publishTime=1689000002000;'
    r's3.tags="\u56fe\u96c6";'
    r's3.title="\u56fe\u96c6";'
    r's3.content="<p></p>";'
    r's3.originPhotoLinks="[{\"raw\":\"https://imglf1.lf1.net/a.jpg\",'
    r'\"orign\":\"https://imglf1.lf1.net/a.jpg?imageView\"}]";'
)


@check("l13 原始数据解析（字段 / tag 小写化 / 图片挑选）")
def t_l13_parse():
    from lofter.config import LikeShareTagConfig
    from lofter.reporter import NullReporter
    from lofter.tasks.like_share_tag import format_entries
    from lofter.tasks.base import TaskContext

    with tempfile.TemporaryDirectory() as tmp:
        cfg = LikeShareTagConfig(mode="tag", url="https://www.lofter.com/tag/x/total")
        ctx = TaskContext.__new__(TaskContext)  # 只需要 reporter
        ctx.reporter = NullReporter()
        favs = DWR_BLOB.split("\n\n")
        format_entries(ctx, cfg, favs, DWR_BLOB, tmp)

        blogs = json.load(open(os.path.join(tmp, "format_blogs_info.json"), encoding="utf-8"))
        expect(len(blogs) == 3, "应解析出 3 条，实际 {}".format(len(blogs)))
        first = blogs[0]
        expect(first["title"] == "长文章", "标题解码失败：{!r}".format(first["title"]))
        expect(first["author name"] == "作者甲", "作者名解码失败：{!r}".format(first["author name"]))
        expect(first["tags"] == ["标签a", "标签b"], "tag 应小写化：{}".format(first["tags"]))
        expect(first["author ip"] == "writer", "三级域名解析失败：{!r}".format(first["author ip"]))
        expect(blogs[1]["tags"] == [] and blogs[1]["title"] == "", "空 tag / 空标题处理异常")
        expect(blogs[2]["img urls"] == ["https://imglf1.lf1.net/a.jpg"],
               "图片链接挑选失败：{}".format(blogs[2]["img urls"]))
    return "3 条记录：标题/作者/tag/图片链接全部正确"


@check("l13 分类 / key tag / tag 统计")
def t_l13_classify():
    from lofter.tasks.like_share_tag import (article_folder, classify, count_tag, count_type,
                                             update_key_tag)
    from lofter.config import LikeShareTagConfig

    blogs = [
        {"img urls": ["a.jpg"], "long article content": "", "title": "t", "tags": ["插画", "原创"],
         "key tag": ""},
        {"img urls": [], "long article content": "", "title": "t2", "tags": ["漫画"], "key tag": ""},
        {"img urls": [], "long article content": "", "title": "", "tags": [], "key tag": ""},
        {"img urls": [], "long article content": "正文", "title": "t3", "tags": ["原创"],
         "key tag": ""},
    ]
    update_key_tag(blogs, True, ["漫画"], True)
    expect(blogs[0]["key tag"] == "other", "未命中优先 tag 且开了聚合应为 other")
    expect(blogs[1]["key tag"] == "漫画", "命中优先 tag 应为优先 tag 本身")
    expect(blogs[2]["key tag"] == "no tag", "无 tag 应标记 no tag")

    classified = classify(blogs)
    counts = count_type(classified)
    expect(counts == {"img": 1, "article": 1, "long article": 1, "text": 1},
           "分类结果异常：{}".format(counts))

    stats = count_tag(blogs)
    expect(list(stats.items())[0][1] == 2, "tag 统计应按次数降序：{}".format(stats))

    cfg = LikeShareTagConfig(classify_by_tag=True, prior_tags=["漫画"], agg_non_prior_tag=True)
    path = article_folder("/base", blogs[1], cfg, "article")
    expect(path.replace("\\", "/") == "/base/article/prior/漫画", "目录拼接错误：{}".format(path))
    cfg2 = LikeShareTagConfig(classify_by_tag=False)
    expect(article_folder("/base", blogs[0], cfg2, "img").replace("\\", "/") == "/base/img",
           "未开启分类时应直接落在 img/")
    return "分类 / key tag / 目录规则 / 统计全部正确"


@check("l13 请求参数构造与翻页更新")
def t_l13_data():
    from lofter.tasks.like_share_tag import make_data, update_data
    from lofter.errors import ConfigError

    class FakeSession:
        headers = {}

    session = FakeSession()
    data = make_data("tag", "https://www.lofter.com/tag/%E6%80%AA%E6%83%B3%E5%94%AE/total",
                     session=session, reporter=None)
    expect(data["c0-scriptName"] == "TagBean" and data["c0-param0"] == "string:%E6%80%AA%E6%83%B3%E5%94%AE",
           "tag 模式参数构造错误：{}".format(data))
    expect(data["c0-param3"] == "string:total", "榜单类型解析错误")
    data = update_data("tag", data, 100, 200, "1689000000000")
    expect(data["c0-param6"] == "number:100" and data["c0-param8"] == "number:1689000000000",
           "翻页参数未更新：{}".format(data))

    d2 = make_data("like2", "", session=session, reporter=None)
    expect(d2["c0-methodName"] == "getFavTrackItem", "like2 模式参数错误")
    try:
        make_data("tag", "", session=session, reporter=None)
        raise AssertionError("空链接应报 ConfigError")
    except ConfigError:
        pass
    return "tag / like2 参数正确，空链接被拦截"


# =================================================================== 6. 配置
@check("配置 FIELDS 与 dataclass 字段完全一致")
def t_config_consistency():
    from dataclasses import fields as dc_fields

    from lofter.config import CONFIG_CLASSES

    problems = []
    for tid, cls in CONFIG_CLASSES.items():
        declared = set(cls.FIELDS and [f.name for f in cls.FIELDS])
        actual = {f.name for f in dc_fields(cls)}
        if declared != actual:
            problems.append("{} 缺 FIELDS:{} 缺字段:{}".format(
                tid, sorted(actual - declared), sorted(declared - actual)))
    expect(not problems, "；".join(problems))
    return "{} 个配置类声明与字段一致".format(len(CONFIG_CLASSES))


@check("配置读写往返 + 类型强制 + 非法值拦截")
def t_config_roundtrip():
    from lofter.config import LikeShareTagConfig
    from lofter.errors import ConfigError

    cfg = LikeShareTagConfig()
    cfg.mode = "like2"
    cfg.url = "https://x.lofter.com/"
    cfg.prior_tags = ["漫画", "原创"]
    cfg.tag_filt_num = 3
    cfg.save_img = False
    restored = LikeShareTagConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    expect(restored.to_dict() == cfg.to_dict(), "往返后配置不一致")

    # 界面/命令行传进来的都是字符串，靠 coerce 转类型
    restored.set("tag_filt_num", "77")
    expect(restored.tag_filt_num == 77, "字符串→int 失败")
    restored.set("save_img", "是")
    expect(restored.save_img is True, "中文布尔值识别失败")
    restored.set("prior_tags", "a\n\nb\n")
    expect(restored.prior_tags == ["a", "b"], "多行→list 失败：{}".format(restored.prior_tags))
    try:
        restored.set("tag_filt_num", "abc")
        raise AssertionError("非法整数应报 ConfigError")
    except ConfigError:
        pass
    try:
        restored.set("mode", "nope")
        raise AssertionError("非法枚举应报 ConfigError")
    except ConfigError:
        pass
    # 未知字段应被忽略而不是报错（向前兼容）
    odd = LikeShareTagConfig.from_dict({"mode": "tag", "未来字段": 1})
    expect(odd.mode == "tag", "未知字段应被忽略")
    expect(LikeShareTagConfig.from_dict({"tag_filt_num": "abc"}).tag_filt_num == 50,
           "非法值应退回默认值")
    return "往返一致；int/bool/list 强制正确；非法值被拦截或退回默认"


@check("配置落盘 / 读取（settings.json）")
def t_config_persist():
    from lofter import config as cfg_mod

    with tempfile.TemporaryDirectory() as tmp:
        old_dir, old_file = cfg_mod.CONFIG_DIR, cfg_mod.SETTINGS_FILE
        cfg_mod.CONFIG_DIR = __import__("pathlib").Path(tmp)
        cfg_mod.SETTINGS_FILE = cfg_mod.CONFIG_DIR / "settings.json"
        try:
            cfg = cfg_mod.LikeShareTagConfig(mode="share", url="https://x/")
            cfg_mod.save_task_configs({"like_share_tag": cfg})
            loaded = cfg_mod.load_task_configs()["like_share_tag"]
            expect(loaded.mode == "share" and loaded.url == "https://x/", "落盘后被读错")
            cfg_mod.save_app_state({"last_task": "blogs", "geometry": "1x1+0+0"})
            expect(cfg_mod.load_app_state()["last_task"] == "blogs", "app state 读写失败")
            cfg_mod.save_task_configs({"like_share_tag": cfg})
            expect(cfg_mod.load_app_state()["last_task"] == "blogs", "保存任务配置覆盖了 app state")
        finally:
            cfg_mod.CONFIG_DIR, cfg_mod.SETTINGS_FILE = old_dir, old_file
    return "任务配置与界面状态互不覆盖"


@check("旧 login_info.py 自动迁移（ast 静态解析，不 exec）")
def t_migrate_login():
    from lofter import config as cfg_mod

    with tempfile.TemporaryDirectory() as tmp:
        login_file = os.path.join(tmp, "login_info.py")
        with open(login_file, "w", encoding="utf-8") as fp:
            fp.write('"""doc"""\n'
                     'login_key = "LOFTER_SESS"\n'
                     'login_auth = "abc123"\n'
                     'evil = __import__("os").system("echo pwned")\n')
        old_root = cfg_mod.PROJECT_ROOT
        cfg_mod.PROJECT_ROOT = __import__("pathlib").Path(tmp)
        try:
            account = cfg_mod.migrate_account_from_login_info()
        finally:
            cfg_mod.PROJECT_ROOT = old_root
        expect(account is not None, "应能迁移出账号")
        expect(account.login_key == "LOFTER_SESS" and account.login_auth == "abc123",
               "迁移结果错误：{}".format(account))
    return "login_key/login_auth 正确迁移，可执行语句未被运行"


@check("账号校验：空 login_auth 必须被拦下")
def t_account_check():
    from lofter.config import Account
    from lofter.errors import AuthError

    account = Account()
    expect(not account.ready, "空账号不该 ready")
    try:
        account.check()
        raise AssertionError("空 login_auth 应抛 AuthError")
    except AuthError:
        pass
    account.login_auth = "x"
    expect(account.cookie_dict() == {"LOFTER-PHONE-LOGIN-AUTH": "x"}, "cookie 字典构造错误")
    return "AuthError 正常抛出"


@check("字段声明都能被 GUI 认识（kind 取值合法）")
def t_field_kinds():
    from lofter.config import CONFIG_CLASSES

    valid = {"str", "int", "bool", "choice", "list", "text", "json", "path", "secret"}
    problems = []
    for tid, cls in CONFIG_CLASSES.items():
        for spec in cls.FIELDS:
            if spec.kind not in valid:
                problems.append("{}.{}={}".format(tid, spec.name, spec.kind))
            if spec.kind == "choice" and not spec.choices:
                problems.append("{}.{} 缺 choices".format(tid, spec.name))
    expect(not problems, "；".join(problems))
    return "全部 kind 合法、choice 均带候选值"


# =================================================================== 7. 进度
@check("StageFiles 进度语义 + 重置")
def t_progress():
    from lofter.progress import StageFiles

    with tempfile.TemporaryDirectory() as tmp:
        stages = StageFiles(tmp)
        expect(stages.describe()["阶段1_完成"] is False, "初始不该有进度")
        stages.write_json(StageFiles.FORMATTED, [{"url": "x"}])
        expect(stages.exists(StageFiles.FORMATTED), "写进度失败")
        expect(stages.read_json(StageFiles.FORMATTED) == [{"url": "x"}], "读进度失败")
        os.makedirs(os.path.join(tmp, "article"))
        removed = stages.reset_all_files()
        expect(removed == [StageFiles.FORMATTED], "重置应只删文件：{}".format(removed))
        expect(os.path.isdir(os.path.join(tmp, "article")), "重置不该删目录")
        expect(StageFiles(tmp).read_json("不存在.json", {"d": 1}) == {"d": 1}, "默认值无效")
    return "写/读/描述/重置（保留子目录）均正确"


@check("UTF-8 编码：中文进度文件读写不出现乱码")
def t_encoding():
    from lofter.progress import read_json, read_text, write_json, write_text

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "中文.json")
        write_json(path, {"作者": "作者甲", "标签": ["漫画", "原创"]})
        raw = open(path, "rb").read().decode("utf-8")
        expect("作者甲" in raw, "落盘不是 UTF-8")
        expect(read_json(path)["作者"] == "作者甲", "读回乱码")
        text_path = os.path.join(tmp, "笔记.txt")
        write_text(text_path, "中文内容")
        expect(read_text(text_path) == "中文内容", "文本读写乱码")
    return "json / 文本均为 UTF-8，中文无损"


# ================================================================ 8. Reporter
@check("Reporter：日志等级 / 确认默认值 / 取消信号")
def t_reporter():
    from lofter.errors import TaskCancelled
    from lofter.reporter import CliReporter, NullReporter

    buffer = io.StringIO()
    cli = CliReporter(verbose=False, stream=buffer)
    cli.debug("不该出现")
    cli.log("该出现")
    expect("该出现" in buffer.getvalue() and "不该出现" not in buffer.getvalue(),
           "日志等级过滤失效：{}".format(buffer.getvalue()))

    null = NullReporter(answers=["ok", "no"])
    expect(null.ask("问题？") == "ok", "ask 未按脚本回答")
    expect(null.confirm("继续？") is False, "confirm 未按脚本回答")
    expect(null.confirm("没人回答？", default=True) is True, "无回答时应返回默认值")

    null.request_cancel()
    try:
        null.check_cancel()
        raise AssertionError("应抛 TaskCancelled")
    except TaskCancelled:
        pass
    return "等级过滤 / 问答 / 取消均正确"


@check("GUI 跨线程提问握手（QueueReporter）")
def t_queue_reporter():
    import queue as _queue

    from gui.worker import QueueReporter
    from lofter.errors import TaskCancelled

    events: "_queue.Queue" = _queue.Queue()
    seen: list[str] = []
    reporter = QueueReporter(events)
    reporter.log("hi")
    reporter.stage("阶段X")
    reporter.progress(1, 10, "半程")

    def answer() -> None:
        while True:
            event = events.get(timeout=3)
            seen.append(event[0])
            if event[0] == "confirm":
                event[3].put(True)
                return

    worker = threading.Thread(target=answer, daemon=True)
    worker.start()
    expect(reporter.confirm("继续？") is True, "主线程回答没有传回子线程")
    worker.join(timeout=3)

    expect([k for k in seen if k != "log"] == ["stage", "progress", "confirm"],
           "事件顺序/类型不符：{}".format(seen))
    expect(seen.count("log") >= 1, "日志事件没投递：{}".format(seen))

    # 取消时不能永久阻塞在等回答上
    stalled = QueueReporter(_queue.Queue())
    stalled.request_cancel()
    try:
        stalled.ask("没人回答的问题", default="x")
        raise AssertionError("已取消时提问应抛 TaskCancelled")
    except TaskCancelled:
        pass
    return "事件投递顺序正确，跨线程回答与取消都不卡死"


# =================================================== 9. l13 离线端到端
@check("l13 阶段2/3/4 离线端到端（关掉全部保存项）")
def t_l13_e2e():
    from lofter.config import LikeShareTagConfig
    from lofter.progress import read_json, write_json
    from lofter.reporter import NullReporter
    from lofter.tasks import get_task
    from lofter.tasks.base import TaskContext

    with tempfile.TemporaryDirectory() as tmp:
        cfg = LikeShareTagConfig(
            mode="like1", url="https://writer.lofter.com/", base_dir=tmp,
            classify_by_tag=True, prior_tags=["漫画"], agg_non_prior_tag=True,
            save_article=False, save_text=False, save_long_article=False, save_img=False,
            save_img_in_text=False, pause_before_save=False, reset_after_save=False,
            tag_filt_num=0,
        )
        task = get_task("like_share_tag")
        file_path = task.base_dir_of(cfg)           # <tmp>/like1_file
        os.makedirs(file_path, exist_ok=True)

        # 手工造一份阶段1产物，等于「阶段1已跑完」，于是整条链路纯本地跑
        from lofter.reporter import NullReporter as _NR
        from lofter.tasks.like_share_tag import format_entries
        ctx_stub = TaskContext.__new__(TaskContext)
        ctx_stub.reporter = _NR()
        format_entries(ctx_stub, cfg, DWR_BLOB.split("\n\n"), DWR_BLOB, file_path)

        reporter = NullReporter()
        reporter.log("")  # 触发一下
        ctx = TaskContext.__new__(TaskContext)
        ctx.account = None
        ctx.reporter = reporter
        ctx.base_dir = file_path

        task.run(ctx, cfg)

        classified = read_json(os.path.join(file_path, "classified_blogs_info.json"))
        expect(classified is not None, "阶段2 没产出 classified_blogs_info.json")
        expect(len(classified["article"]) == 1, "文章分类数不对：{}".format(len(classified["article"])))
        expect(len(classified["text"]) == 1, "文本分类数不对：{}".format(len(classified["text"])))
        expect(len(classified["img"]) == 1, "图片分类数不对：{}".format(len(classified["img"])))
        expect(classified["article"][0]["key tag"] == "other", "key tag 未写入阶段2产物")
        expect(classified["img"][0]["key tag"] == "other", "图片条目 key tag 错误")
        expect(os.path.exists(os.path.join(tmp, "prior_tags.txt")), "没生成 prior_tags.txt")
        stages_hit = [s for s in reporter.stages]
        expect(any("阶段2" in s for s in stages_hit) and any("阶段4" in s for s in stages_hit),
               "阶段事件不全：{}".format(stages_hit))
    return "阶段2分类产出正确，阶段3统计与阶段4跳过逻辑跑通"


@check("取消令牌能在长循环中生效")
def t_cancel():
    from lofter.errors import TaskCancelled
    from lofter.reporter import NullReporter

    reporter = NullReporter()
    reporter.request_cancel()
    try:
        reporter.check_cancel()
        raise AssertionError("应抛 TaskCancelled")
    except TaskCancelled:
        pass
    return "TaskCancelled 正常抛出"


# ===================================================================== 10. CLI
@check("CLI：--help / --list / --show-config / 参数校验")
def t_cli():
    import contextlib

    from cli.run import main as cli_main

    def run(*args):
        """在进程内跑 CLI（避免起子进程，也便于断言退出码）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                code = cli_main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, buffer.getvalue()

    code, out = run("--help")
    expect(code == 0 and "--task" in out, "--help 失败：{}".format(out[:200]))

    code, out = run("--list")
    expect(code == 0 and "like_share_tag" in out, "--list 失败")
    expect(all(tid in out for tid in ("author_img", "author_txt", "blogs", "homepage", "phone_tag")),
           "--list 任务不全：{}".format(out[:300]))

    code, out = run("--show-config", "like_share_tag")
    expect(code == 0 and "pause_before_save" in out and "save_img" in out, "--show-config 失败")
    expect("login_auth" not in out, "--show-config 不该打印登录凭证字段")

    code, out = run("--show-config")
    expect(code == 0 and "author_img" in out, "--show-config 不带 id 应打印全部")

    code, out = run("--task", "不存在的任务")
    expect(code != 0, "未知任务应当返回非 0")

    code, out = run("--cli")
    expect(code == 2, "无任务时应打印帮助并返回 2，实际 {}".format(code))

    code, out = run("--task", "like_share_tag", "--set", "没有这个键=1")
    expect(code != 0 and "没有配置项" in out, "--set 未知键应报错：{}".format(out[:200]))

    code, out = run("--task", "author_img", "--set", "author_url=https://x.lofter.com",
                    "--set", "tags_filter_mode=乱填")
    expect(code != 0, "非法枚举应报错")
    return "help/list/show-config/未知任务/未知键/非法枚举 全部符合预期"


# ===================================================================== 11. GUI
@check("GUI 无 mainloop 构建（建表 → 切任务 → 销毁）")
def t_gui():
    if NO_GUI:
        return "按 --no-gui 跳过"
    try:
        import tkinter as tk
    except Exception as exc:
        return "跳过（无 tkinter：{}）".format(exc)

    from lofter import config as cfg_mod
    from gui.app import App

    with tempfile.TemporaryDirectory() as tmp:
        old_dir, old_file = cfg_mod.CONFIG_DIR, cfg_mod.SETTINGS_FILE
        old_acc = cfg_mod.ACCOUNT_FILE
        from pathlib import Path

        cfg_mod.CONFIG_DIR = Path(tmp)
        cfg_mod.SETTINGS_FILE = cfg_mod.CONFIG_DIR / "settings.json"
        cfg_mod.ACCOUNT_FILE = cfg_mod.CONFIG_DIR / "account.json"
        app = None
        try:
            try:
                # 无显示环境（纯终端 Linux / 远程会话）会在这里抛 TclError；
                # 不要先建一个探测窗口，ttk 的 Style 单例缓存了第一个 root，
                # 反复创建销毁会引发 "application has been destroyed" 噪音。
                app = App()
            except tk.TclError as exc:
                return "跳过（无显示环境：{}）".format(exc)
            app.withdraw()
            app.update()
            built = []
            for task in __import__("lofter.tasks", fromlist=["TASKS"]).TASKS:
                app.select_task(task.tid)
                app.update()
                expect(app.form is not None, "{} 没建出表单".format(task.tid))
                expect(len(app.form._widgets) == len(app.configs[task.tid].FIELDS),
                       "{} 表单控件数与 FIELDS 不一致".format(task.tid))
                built.append("{}:{}".format(task.tid, len(app.form._widgets)))
            app._pump()
            app.update()
            expect("输出目录" in app.dir_label.cget("text"), "输出目录标签未刷新")

            # 主题切换必须真的重绘：ttk 走 style，而 Canvas/Text/Listbox 不吃 style，
            # 只改 ttk 样式的话这几处会「深色窗口配亮色控件」。
            light_list_bg = app.task_list.cget("background")
            light_log_bg = app.log.text.cget("background")
            app._change_theme("深色")
            app.update()
            expect(app._theme.palette["bg"] == "#1e1f22", "深色调色板没生效")
            expect(app.task_list.cget("background") != light_list_bg,
                   "切深色后任务列表背景没变（Canvas/Listbox 没走 apply_palette）")
            expect(app.log.text.cget("background") != light_log_bg,
                   "切深色后日志面板背景没变")
            expect(app.theme_box.get() == "深色", "主题下拉没同步")
            expect(app.state_.get("theme") == "深色", "主题没写进界面状态")

            app._change_theme("浅色")
            app.update()
            expect(app.task_list.cget("background") == light_list_bg, "切回浅色没还原")
            expect(app.log.text.cget("background") == light_log_bg, "切回浅色日志面板没还原")

            # 统计卡片渲染
            from lofter.tasks.base import TaskResult

            app._render_stats(TaskResult(task="probe", ok=True, output_dir=tmp,
                                         elapsed=1.25, stats={"已保存章节": 3, "跳过": 1}))
            text = app.stats_label.cget("text")
            expect("已保存章节 3" in text and "跳过 1" in text and "耗时 1.2s" in text,
                   "统计卡片渲染不对：{}".format(text))
        finally:
            if app is not None:
                app._closing = True
                app.destroy()
            cfg_mod.CONFIG_DIR, cfg_mod.SETTINGS_FILE = old_dir, old_file
            cfg_mod.ACCOUNT_FILE = old_acc
        return "；".join(built)


# ================================================== 12. 合集下载（离线）
PROBE_COLLECTION_ITEMS = [
    {"post": {"title": "第一章 初遇", "content": "<p>正文一</p>", "type": 1,
              "tagList": ["连载", "测试"], "blogPageUrl": "https://a.lofter.com/post/1_1",
              "publishTime": 1689000000000}, "blogInfo": {"blogNickName": "作者甲"}},
    {"post": {"title": "第二章 再会", "content": "<p>正文二</p>", "type": 1,
              "tagList": ["连载"], "blogPageUrl": "https://a.lofter.com/post/1_2",
              "publishTime": 1689100000000}, "blogInfo": {"blogNickName": "作者甲"}},
    {"post": {"noticeLinkTitle": "第三章 归途", "content": "<p>正文三</p>", "type": 1,
              "tagList": [], "blogPageUrl": "https://a.lofter.com/post/1_3",
              "publishTime": 1689200000000}, "blogInfo": {"blogNickName": "作者甲"}},
]


@check("appapi 响应信封 / 类型容错 / collectionId 提取")
def t_appapi_envelope():
    from lofter.appapi import AppApi, find_collection_id, to_int, unwrap

    expect(unwrap({"response": {"a": 1}}) == {"a": 1}, "response 信封没剥开")
    expect(unwrap({"data": {"b": 2}}) == {"b": 2}, "data 信封没剥开")
    expect(unwrap({"c": 3}) == {"c": 3}, "顶层字段不该被动")
    expect(unwrap(None) == {} and unwrap("x") == {}, "非字典要返回空字典")

    expect(to_int("42") == 42 and to_int(None) == 0 and to_int("abc", -1) == -1,
           "to_int 容错不对")
    expect(to_int(True) == 1, "bool 应转成 1")

    expect(find_collection_id("https://x.lofter.com/post/1?collectionId=123456789") == "123456789",
           "链接参数没识别")
    expect(find_collection_id('"collectionId": 987654') == "987654", "JSON 字段没识别")
    expect(find_collection_id("https://www.lofter.com/front/blog/collection/100200") == "100200",
           "路径形式没识别")
    expect(find_collection_id("没有合集信息") == "", "不该误报")

    expect(AppApi.page_count(0, 50) == 1 and AppApi.page_count(50, 50) == 1
           and AppApi.page_count(51, 50) == 2, "页数计算不对")
    return "三种信封 + 类型容错 + 三种 collectionId 写法"


@check("合集条目解析（字段归一化 / 标题兜底 / 图片型）")
def t_collection_parse_item():
    from lofter.tasks.collection import parse_item, parse_photo_links

    first = parse_item(PROBE_COLLECTION_ITEMS[0], index=1)
    expect(first["title"] == "第一章 初遇", "标题解析失败")
    expect(first["author"] == "作者甲", "作者应从 blogInfo 抬上来")
    expect(first["tags"] == ["连载", "测试"], "tagList 解析失败")
    expect(first["publish_ts"] > 0 and first["publish_time"], "发布时间解析失败")
    expect(first["url"].endswith("/post/1_1"), "文章链接解析失败")

    third = parse_item(PROBE_COLLECTION_ITEMS[2], index=3)
    expect(third["title"] == "第三章 归途", "title 为空时应退回 noticeLinkTitle")

    # blogInfo 嵌在 post 里也要能拿到
    nested = parse_item({"post": {"title": "t", "content": "x",
                                  "blogInfo": {"blogNickName": "作者乙"}}})
    expect(nested["author"] == "作者乙", "嵌套的 blogInfo 没抬上来")

    # 图片型：photoLinks 是 JSON 字符串，正文只有配文，图要补到前面
    image_item = {"post": {
        "title": "图集", "content": "配文", "type": 2,
        "photoLinks": '[{"orign": "https://imglf1.lf1.net/a.jpg?imageView", '
                      '"raw": "https://imglf1.lf1.net/a.jpg"}, '
                      '{"orign": "https://imglf1.lf1.net/b.png"}]'}}
    image_record = parse_item(image_item, index=1)
    expect(image_record["photo_urls"] == ["https://imglf1.lf1.net/a.jpg",
                                          "https://imglf1.lf1.net/b.png"],
           "photoLinks 解析失败：{}".format(image_record["photo_urls"]))
    expect(image_record["content_html"].index("<img") < image_record["content_html"].index("配文"),
           "图片应补在配文前面")

    expect(parse_photo_links("") == [], "空值应返回空列表")
    expect(parse_photo_links("不是列表") == [], "非法输入应返回空列表")
    expect(parse_photo_links([{"raw": "https://x/c.jpg"}, "https://x/d.jpg"])
           == ["https://x/c.jpg", "https://x/d.jpg"], "混合元素解析失败")
    return "标题兜底 / 嵌套 blogInfo / 图片型补图 / photoLinks 三种形态"


@check("合集正文转换：不折行、txt 降级、md 保留图片")
def t_collection_html2text():
    from lofter.tasks.collection import html_to_content

    long_para = "这是一段很长的中文段落，" * 12
    html = "<p>{}</p><p><img src=\"https://imglf1.lf1.net/a.jpg\"/></p>".format(long_para)

    text = html_to_content(html, "txt")
    expect("这是一段很长的中文段落" in text, "正文丢失")
    expect("[图片] https://imglf1.lf1.net/a.jpg" in text,
           "txt 模式应把图片降级成 [图片] url：{}".format(text[-120:]))
    expect(text.count("\n") <= 3,
           "不该按 78 列硬折行（中文会被切碎），实际换行 {} 次".format(text.count("\n")))

    markdown = html_to_content(html, "md")
    expect("](https://imglf1.lf1.net/a.jpg)" in markdown,
           "md 模式应保留图片语法：{}".format(markdown[-120:]))
    expect("[图片]" not in markdown, "md 模式不该降级")
    expect(html_to_content("", "txt") == "", "空输入应返回空串")
    return "长段落不折行 / txt 降级 / md 保留 ![]()"


@check("合集章节排序与文件名补零")
def t_collection_sort_filename():
    from lofter.tasks.collection import chapter_filename, sort_records

    records = [{"title": "c", "publish_ts": 300},
               {"title": "a", "publish_ts": 100},
               {"title": "no-time", "publish_ts": 0},
               {"title": "b", "publish_ts": 200}]
    expect([r["title"] for r in sort_records(records, "asc")] == ["a", "b", "c", "no-time"],
           "正序排序错误")
    expect([r["title"] for r in sort_records(records, "desc")] == ["c", "b", "a", "no-time"],
           "倒序排序错误")
    expect([r["title"] for r in sort_records(records, "api")]
           == ["c", "a", "no-time", "b"], "api 模式应保持原顺序")
    expect(records[0]["title"] == "c", "排序不该改动入参")

    expect(chapter_filename(1, "初遇", ".txt") == "001_初遇.txt", "文件名补零错误")
    expect(chapter_filename(12, "a/b:c", ".md") == "012_a&b：c.md", "文件名清洗错误")
    expect(chapter_filename(3, "", ".txt") == "003_无标题.txt", "空标题应有兜底名")
    return "三种排序模式 + 补零 + 非法字符清洗"


@check("合集元信息解析（缺失时可读报错）")
def t_collection_meta():
    from lofter.appapi import AppApi
    from lofter.errors import ParseError

    class _Stub(AppApi):
        def __init__(self, payload):  # noqa: D107 - 测试替身
            super().__init__()
            self.payload = payload

        def collection_page(self, collection_id, offset=0, limit=50, order=1):
            return self.payload

    api = _Stub({"collection": {"id": 7, "name": " 测试合集 ", "postCount": "3",
                                "blogId": 99, "tags": "连载,测试",
                                "description": "简介"},
                 "blogInfo": {"blogNickName": "作者甲"}})
    meta = api.collection_meta("7")
    expect(meta["name"] == "测试合集", "合集名没去空格")
    expect(meta["post_count"] == 3, "postCount 字符串没转 int")
    expect(meta["tags"] == ["连载", "测试"], "tags 没切成列表")
    expect(meta["author"] == "作者甲", "作者没取到")

    try:
        _Stub({}).collection_meta("7")
        raise AssertionError("没有 collection 字段时应抛 ParseError")
    except ParseError as exc:
        expect("collectionId" in str(exc) or "合集" in str(exc), "报错信息应给出排查方向")
    return "postCount 容错 / tags 切分 / 缺字段报错可读"


@check("合集分页迭代（不触碰网络）")
def t_collection_paging():
    from lofter.appapi import AppApi

    class _Paged(AppApi):
        def __init__(self, total, page_size_api=2):  # noqa: D107 - 测试替身
            super().__init__()
            self.total = total
            self.calls = []
            self._size = page_size_api

        def collection_page(self, collection_id, offset=0, limit=50, order=1):
            self.calls.append((offset, limit))
            chunk = list(range(offset, min(offset + limit, self.total)))
            return {"items": [{"post": {"title": "第{}章".format(i)}, "blogInfo": {}} for i in chunk]}

    api = _Paged(total=5)
    pages = list(api.iter_collection_pages("1", page_size=2, post_count=5))
    expect([len(p) for p in pages] == [2, 2, 1], "分页切分错误：{}".format([len(p) for p in pages]))
    expect(api.calls == [(0, 2), (2, 2), (4, 2)], "翻页 offset 不对：{}".format(api.calls))

    api2 = _Paged(total=5)
    items = api2.collection_items("1", page_size=2, max_items=3)
    expect(len(items) == 3, "max_items 没生效：{}".format(len(items)))
    expect(api2.calls == [(0, 2), (2, 2)], "max_items 应在拿满后停止请求：{}".format(api2.calls))

    api3 = _Paged(total=0)
    expect(list(api3.iter_collection_pages("1", page_size=2)) == [], "空合集不该有页")
    return "分页切分 / offset 递进 / max_items 提前停止 / 空合集"


@check("合集下载离线端到端（假接口 → 真落盘 → 断点续跑）")
def t_collection_e2e():
    from lofter import tasks as tasks_pkg
    from lofter.config import Account, CollectionConfig
    from lofter.progress import read_json
    from lofter.reporter import NullReporter
    from lofter.tasks import collection as collection_module

    class _FakeApi:
        """替身：不联网，但接口形状与真实 AppApi 一致。"""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def collection_meta(self, collection_id):
            return {"id": collection_id, "name": "测试合集", "post_count": 3,
                    "blog_id": "1", "author": "作者甲", "tags": ["连载"],
                    "description": "一个用于测试的合集"}

        def page_count(self, post_count, page_size):
            return 1

        def iter_collection_pages(self, collection_id, **kwargs):
            yield PROBE_COLLECTION_ITEMS

        def post_detail(self, blog_id, post_id):
            return {}

    original = collection_module.AppApi
    collection_module.AppApi = _FakeApi
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = CollectionConfig(
                source="collection_id", collection_id="123456", base_dir=tmp,
                save_format="txt", save_img=False, include_tags=True, add_nav=True,
                save_index=True, save_json=True, merge_into_one=True,
                skip_existing=True, merge_filename="完整版",
            )
            account = Account()
            account.login_auth = "probe-token"
            reporter = NullReporter()
            ctx = tasks_pkg.TaskContext(account, reporter, tmp)
            task = tasks_pkg.get_task("collection")

            result = task.run(ctx, cfg)
            collection_dir = os.path.join(tmp, "collection", "测试合集")
            expect(os.path.isdir(collection_dir), "合集目录没建出来：{}".format(collection_dir))

            for name in ("001_第一章 初遇.txt", "002_第二章 再会.txt", "003_第三章 归途.txt"):
                path = os.path.join(collection_dir, name)
                expect(os.path.exists(path), "章节文件缺失：{}".format(name))
                body = open(path, "r", encoding="utf-8").read()
                expect("正文" in body, "{} 里没有正文".format(name))
                expect("原文链接" in body and "合集：" in body, "{} 缺头信息".format(name))
            expect("tag：连载、测试" in open(os.path.join(collection_dir, "001_第一章 初遇.txt"),
                                            encoding="utf-8").read(), "tag 没写进章节")
            expect(os.path.exists(os.path.join(collection_dir, "测试合集 目录.txt")), "目录文件缺失")
            expect(os.path.exists(os.path.join(collection_dir, "测试合集.json")), "JSON 缺失")
            merged = os.path.join(collection_dir, "测试合集 完整版.txt")
            expect(os.path.exists(merged), "合并版缺失")
            expect("第一章 初遇" in open(merged, encoding="utf-8").read(), "合并版内容不全")

            expect(result is not None and result.ok, "任务应返回成功的 TaskResult")
            expect(result.stats.get("已保存章节") == 3,
                   "统计里应有 3 章：{}".format(result.stats))
            expect(result.stats.get("章节总数") == 3, "章节总数统计缺失")

            # 断点续跑：再跑一次应该全部命中「已存在跳过」
            reporter2 = NullReporter()
            ctx2 = tasks_pkg.TaskContext(account, reporter2, tmp)
            result2 = task.run(ctx2, cfg)
            expect(result2.stats.get("已存在跳过") == 3,
                   "第二次运行应跳过 3 章：{}".format(result2.stats))
            expect(result2.stats.get("已保存章节") is None, "第二次不该再保存新章节")

            # 标题过滤
            cfg3 = CollectionConfig(source="collection_id", collection_id="123456",
                                    base_dir=tmp, save_img=False, skip_existing=False,
                                    save_index=False, save_json=False, title_filter="第二章")
            reporter3 = NullReporter()
            ctx3 = tasks_pkg.TaskContext(account, reporter3, tmp)
            result3 = task.run(ctx3, cfg3)
            expect(result3.stats.get("标题过滤跳过") == 2,
                   "标题过滤应跳过 2 章：{}".format(result3.stats))
            expect(result3.stats.get("已保存章节") == 1, "标题过滤应只保存 1 章")
    finally:
        collection_module.AppApi = original
    return "3 章落盘 + 目录/JSON/合并版 + 断点续跑 + 标题过滤"


@check("合集配置校验与选项映射")
def t_collection_config():
    from lofter.config import CollectionConfig
    from lofter.errors import ConfigError

    cfg = CollectionConfig()
    expect(cfg.file_suffix == ".txt" and cfg.sort_mode == "api", "默认值不对")
    cfg.save_format = "md"
    expect(cfg.file_suffix == ".md", "md 后缀不对")
    for text, mode in (("自动（接口顺序）", "api"), ("正序（从旧到新）", "asc"),
                       ("倒序（从新到旧）", "desc")):
        cfg.chapter_order = text
        expect(cfg.sort_mode == mode, "{} 应映射成 {}".format(text, mode))

    for bad, why in (
        (CollectionConfig(source="collection_id", collection_id=""), "空合集 ID"),
        (CollectionConfig(source="article", url=""), "空文章链接"),
        (CollectionConfig(source="article", url="not-a-url"), "链接不以 http 开头"),
        (CollectionConfig(source="collection_id", collection_id="1", page_size=0), "页大小为 0"),
    ):
        try:
            bad.validate()
            raise AssertionError("{} 应该报错".format(why))
        except ConfigError:
            pass
    CollectionConfig(source="collection_id", collection_id="123").validate()
    return "后缀/排序映射 + 四类非法配置全部被拦下"


# ============================================ 13. 本轮改进项（v2.2）
@check("合集目录的 Markdown 链接转义（标题含空格 / # / () / []）")
def t_md_escape():
    import re as _re
    from urllib.parse import unquote

    from lofter.config import CollectionConfig
    from lofter.tasks import collection as cm

    with tempfile.TemporaryDirectory() as tmp:
        cfg = CollectionConfig(save_format="md")
        meta = {"name": "测试合集", "author": "作者甲", "tags": [], "description": ""}
        records = [{"title": "第 1 章（上） #引用 [重要]", "tags": []},
                   {"title": "a&b(c) 2", "tags": []}]
        filenames = [cm.chapter_filename(i, r["title"], ".md")
                     for i, r in enumerate(records, 1)]
        path = cm.save_index(tmp, cfg, meta, records, filenames)
        with open(path, encoding="utf-8") as fp:
            text = fp.read()

        targets = _re.findall(r"\]\(([^)]+)\)", text)
        expect(len(targets) == len(records), "目录链接数不对：{}".format(targets))
        for target in targets:
            expect(" " not in target and "#" not in target,
                   "链接目标里的特殊字符没转义：{}".format(target))
            expect("(" not in target and ")" not in target,
                   "半角括号会提前闭合链接：{}".format(target))
            decoded = unquote(target)
            expect(decoded in filenames, "链接目标解出来对不上文件：{}".format(decoded))
        expect("\\[" in text and "\\]" in text,
               "链接文字里的 [] 必须转义，否则链接会错位")

        # 纯文本模式不产生 Markdown 语法，不该被转义逻辑污染
        cfg_txt = CollectionConfig(save_format="txt")
        path2 = cm.save_index(tmp, cfg_txt, meta, records, filenames)
        with open(path2, encoding="utf-8") as fp:
            text2 = fp.read()
        expect("](" not in text2, "txt 模式不该出现 Markdown 链接语法")
        expect("第 1 章（上） #引用 [重要]" in text2, "txt 模式应保留原始标题")
    return "链接目标 percent-encode、链接文字转义、txt 模式不受影响"


@check("并发下载：调用次数守恒（防一个任务下多张图的回归）")
def t_download_many():
    from lofter.download import download_many
    from lofter.reporter import NullReporter

    calls: list = []

    def fake(url):
        calls.append(url)
        return b"data:" + url.encode()

    urls = ["https://x/{}.jpg".format(i) for i in range(7)]
    report = download_many(urls, fake, workers=4, reporter=NullReporter(), desc="测试")
    expect(report.calls == len(urls),
           "下载次数必须等于图片数（这就是参考项目那个 bug 的回归哨兵）：{}".format(report.calls))
    expect(len(calls) == len(urls), "fetcher 实际调用次数不对：{}".format(len(calls)))
    expect(report.ok_count == 7 and report.failed_count == 0, "成功数不对")
    expect(sorted(report.contents) == sorted(urls), "内容映射与 url 不匹配")
    expect(report.contents[urls[0]] == b"data:" + urls[0].encode(), "内容张冠李戴了")

    def partially_broken(url):
        if url.endswith("1.jpg"):
            raise RuntimeError("boom")
        return b"ok"

    report2 = download_many(urls, partially_broken, workers=3)
    expect(report2.failed_count == 1, "单张失败应被记录")
    expect("boom" in report2.errors[urls[1]], "失败原因应保留：{}".format(report2.errors))
    expect(report2.ok_count == 6, "单张失败不该影响其它")

    report3 = download_many(["a", "a", "b"], fake, workers=2)
    expect(report3.calls == 2, "重复 url 应去重：{}".format(report3.calls))
    expect(download_many([], fake).total == 0, "空输入应返回空报告")

    reporter = NullReporter()
    reporter.request_cancel()
    try:
        download_many(urls, fake, workers=2, reporter=reporter)
        raise AssertionError("已取消时应抛 TaskCancelled")
    except TaskCancelled:
        pass
    return "次数守恒 / 内容对应 / 失败隔离 / 去重 / 空输入 / 取消"


@check("合集图片本地化：命名带自己的序号、正文替换、失败保留原链接")
def t_collection_images():
    from lofter.config import Account, CollectionConfig
    from lofter.errors import NetworkError
    from lofter.reporter import NullReporter
    from lofter.tasks import collection as cm
    from lofter.tasks.base import TaskContext

    with tempfile.TemporaryDirectory() as tmp:
        cfg = CollectionConfig(save_img=True, img_workers=4, save_format="md")
        account = Account()
        account.login_auth = "probe"
        ctx = TaskContext(account, NullReporter(), tmp)
        image_dir = os.path.join(tmp, "images")
        record = {
            "title": "带图的章",
            "url": "https://a.lofter.com/post/1_1",
            # 三张图都写进正文，才能验证「成功的换成本地路径、失败的保留原链接」
            "content_html": ('<p>看图</p>'
                             '<img src="https://imglf1.lf1.net/a.jpg"/>'
                             '<img src="https://imglf1.lf1.net/b.png"/>'
                             '<img src="https://imglf1.lf1.net/c.gif"/>'),
            "photo_urls": ["https://imglf1.lf1.net/a.jpg",
                           "https://imglf1.lf1.net/b.png",
                           "https://imglf1.lf1.net/c.gif"],
            "tags": [],
            "author": "作者甲",
            "publish_time": "",
        }
        meta = {"name": "测试合集", "author": "作者甲"}

        original = cm.fetch_bytes
        fetched: list = []

        def fake_fetch(session, url, referer=None, cookies=None, timeout=60):
            fetched.append(url)
            if url.endswith("b.png"):
                raise NetworkError("模拟失败")
            return b"\xff\xd8\xff" + url.encode()

        cm.fetch_bytes = fake_fetch
        try:
            # 走完整入口：下载 + 渲染（渲染是纯函数，同一次下载可渲染两种形态）
            standalone, merged, html_for_epub, absolute = cm.compose_chapter(
                ctx, cfg, record, 3, 3, meta, image_dir)
        finally:
            cm.fetch_bytes = original

        expect(len(fetched) == 3, "每张图只该下载一次：{}".format(fetched))
        expect(len(set(fetched)) == 3, "不该有重复下载")
        expect(len(absolute) == 2, "成功数不对：{}".format(sorted(absolute)))
        expect(os.path.exists(os.path.join(image_dir, "003-01.jpg")),
               "第一张图应命名为 003-01.jpg（章节序号-图片序号）")
        expect(os.path.exists(os.path.join(image_dir, "003-03.gif")),
               "第三张图必须用自己的序号 003-03.gif，不能都落到 _0 上")
        expect(not os.path.exists(os.path.join(image_dir, "003-02.png")),
               "失败的图不该留下空文件")
        expect("images/003-01.jpg" in standalone,
               "正文里的图片应换成本地路径：{}".format(standalone))
        expect("https://imglf1.lf1.net/b.png" in standalone, "失败的图应保留原链接")
        expect("images/003-01.jpg" in html_for_epub, "EPUB 用的 HTML 也要本地化")
        expect(ctx.stats.get("图片成功") == 2 and ctx.stats.get("图片失败") == 1,
               "统计不对：{}".format(ctx.stats))

        # 没有要求合并时不该白渲染一份合并形态
        expect(merged is None, "未开启合并时不该产出合并形态")
        expect("合集：测试合集（第 3 / 3 章）" in standalone,
               "逐章形态应带「合集：…」行：{}".format(standalone[:200]))

        # 要求合并时，章节要换成「书的一章」形态
        cfg_merged = CollectionConfig(save_img=False, save_format="md", merge_into_one=True)
        _, merged2, _, _ = cm.compose_chapter(
            ctx, cfg_merged, record, 3, 3, meta, image_dir)
        expect(merged2 is not None, "开启合并后应产出合并形态")
        expect(merged2.lstrip().startswith("## 第 3 章"), "合并形态标题应是「## 第 N 章」：{}".format(
            merged2[:60]))
        expect("合集：测试合集" not in merged2, "合并形态里不该有重复的「合集：」行")

        # 单个文件模式也要拿到合并形态
        _, merged3, _, _ = cm.compose_chapter(
            ctx, CollectionConfig(save_img=False, save_layout="单个文件"),
            record, 3, 3, meta, image_dir)
        expect(merged3 is not None, "单个文件模式必须产出合并形态")
    return "一图一任务、命名按序号、失败降级、两种形态（逐章 / 合并）正确"


@check("合集批量下载：逐个隔离，一个失败不影响其它")
def t_collection_batch():
    from lofter.config import Account, CollectionConfig
    from lofter.errors import ParseError
    from lofter.reporter import NullReporter
    from lofter.tasks import collection as collection_module
    from lofter.tasks import get_task
    from lofter.tasks.base import TaskContext

    class _FlakyApi:
        """第二个合集故意失败，验证错误隔离。"""

        def __init__(self, *args, **kwargs) -> None:
            pass

        def collection_meta(self, collection_id):
            if collection_id == "222":
                raise ParseError("模拟：这个合集挂了")
            return {"id": collection_id, "name": "合集{}".format(collection_id),
                    "post_count": 1, "blog_id": "1", "author": "作者甲",
                    "tags": [], "description": ""}

        def page_count(self, post_count, page_size):
            return 1

        def iter_collection_pages(self, collection_id, **kwargs):
            yield [{"post": {"title": "第1章", "content": "<p>正文</p>", "type": 1,
                             "blogPageUrl": "https://a.lofter.com/post/1_1"},
                    "blogInfo": {"blogNickName": "作者甲"}}]

    original = collection_module.AppApi
    collection_module.AppApi = _FlakyApi
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = CollectionConfig(source="collection_id",
                                   collection_id=["111", "222", "333"],
                                   base_dir=tmp, save_img=False,
                                   save_index=False, save_json=False)
            expect(cfg.collection_ids == ["111", "222", "333"], "多 ID 解析错误")
            account = Account()
            account.login_auth = "probe"
            ctx = TaskContext(account, NullReporter(), tmp)
            result = get_task("collection").run(ctx, cfg)

            root = os.path.join(tmp, "collection")
            expect(os.path.isdir(os.path.join(root, "合集111 [111]")), "第一个合集应下好")
            expect(not os.path.exists(os.path.join(root, "合集222 [222]")),
                   "第二个合集失败了不该留下目录")
            expect(os.path.isdir(os.path.join(root, "合集333 [333]")),
                   "第三个合集必须继续下载（参考项目在这里会整批中断）")
            expect(result.stats.get("成功合集") == 2, "成功合集数不对：{}".format(result.stats))
            expect(result.stats.get("失败合集") == 1, "失败合集数不对：{}".format(result.stats))
            expect(result.stats.get("待下载合集") == 3, "待下载合集数不对")
    finally:
        collection_module.AppApi = original
    return "3 个目标：2 成功 1 失败，失败不中断后续，统计可读"


@check("按作者 / 按订阅解析目标合集（含失效剔除）")
def t_collection_targets():
    from lofter.config import Account, CollectionConfig
    from lofter.reporter import NullReporter
    from lofter.tasks import collection as cm
    from lofter.tasks.base import TaskContext

    class _Api:
        def author_collections(self, domain):
            expect(domain == "writer", "三级域名应从主页链接里解析出来，收到 {}".format(domain))
            return [{"id": "1", "name": "连载A", "valid": True, "post_count": 3},
                    {"id": "2", "name": "连载B", "valid": True, "post_count": 0}]

        def subscriptions(self):
            return [{"id": "9", "name": "订阅A", "valid": True},
                    {"id": "8", "name": "过期B", "valid": False}]

    account = Account()
    account.login_auth = "probe"

    def new_ctx():
        return TaskContext(account, NullReporter(), tempfile.mkdtemp())

    cfg_author = CollectionConfig(source="author", author_url="https://writer.lofter.com/")
    expect(cfg_author.author_domain == "writer", "域名解析错误：{}".format(cfg_author.author_domain))
    targets = cm.resolve_targets(new_ctx(), cfg_author, _Api())
    expect([t.collection_id for t in targets] == ["1", "2"], "作者合集解析错误")

    cfg_domain = CollectionConfig(source="author", author_url="writer.lofter.com")
    expect(cfg_domain.author_domain == "writer", "不带协议的写法也要能解析")

    ctx_sub = new_ctx()
    targets_sub = cm.resolve_targets(
        ctx_sub, CollectionConfig(source="subscription", skip_subscription_invalid=True), _Api())
    expect([t.collection_id for t in targets_sub] == ["9"], "失效订阅应被剔除")
    expect(ctx_sub.stats.get("失效订阅跳过") == 1, "失效订阅要计入统计（不能悄悄丢）")

    targets_all = cm.resolve_targets(
        new_ctx(), CollectionConfig(source="subscription", skip_subscription_invalid=False), _Api())
    expect(len(targets_all) == 2, "关掉跳过时失效的也要保留")

    # collection_id 的三种写法都要能吃
    expect(CollectionConfig(collection_id="111,222 333\n444").collection_ids
           == ["111", "222", "333", "444"], "逗号/空格/换行混排解析失败")
    expect(CollectionConfig(collection_id="111，222").collection_ids == ["111", "222"],
           "全角逗号也要支持")
    expect(CollectionConfig(collection_id="111").collection_ids == ["111"],
           "老配置里的单个字符串要兼容")
    expect(CollectionConfig(collection_id="a,a,b").collection_ids == ["a", "b"], "应去重")
    return "作者域名解析 / 订阅失效剔除与统计 / 多 ID 三种写法兼容"


@check("EPUB 导出：包内章节文件名用序号、结构可校验")
def t_epub_export():
    import zipfile

    from lofter import epub as epub_export
    from lofter.errors import LofterError

    usable, reason = epub_export.available()
    if not usable:
        return "跳过（未安装 EbookLib：{}）".format(reason)

    expect(epub_export.media_type("a.PNG") == "image/png", "扩展名大小写应兼容")

    # 片段与完整文档是两种东西：ebooklib 要片段，独立查看/校验要完整文档
    fragment = epub_export.normalize_fragment("<p>a<br>b</p>")
    expect("<br/>" in fragment, "不闭合的标签应被规范化成自闭合：{}".format(fragment))
    expect("<body>" not in fragment and "<html" not in fragment,
           "片段里不能带 html/body 外壳：{}".format(fragment))
    expect(epub_export.normalize_fragment("") == "", "空输入应返回空片段")
    xhtml = epub_export.build_xhtml("<p>a<br>b</p>", "标题")
    expect(xhtml.startswith("<?xml"), "应是完整的 XHTML 文档")
    expect("<br/>" in xhtml, "完整文档里的标签同样要自闭合：{}".format(xhtml[:200]))
    expect(epub_export.build_xhtml("", "空").count("<body>") == 1, "空正文也要能生成")

    with tempfile.TemporaryDirectory() as tmp:
        image_dir = os.path.join(tmp, "images")
        os.makedirs(image_dir)
        image_path = os.path.join(image_dir, "001-01.jpg")
        with open(image_path, "wb") as fp:
            fp.write(b"\xff\xd8\xff\xe0fake")
        chapters = [
            {"title": "第 1 章（上） #引用 [重要]",
             "html": '<p>正文一</p><img src="images/001-01.jpg"/>'},
            {"title": "第 2 章", "html": "<p>正文二</p>"},
        ]
        out = os.path.join(tmp, "book.epub")
        epub_export.export_epub(chapters, out, title="测试合集", author="作者甲",
                                images=[("images/001-01.jpg", image_path)])
        expect(os.path.exists(out), "EPUB 没生成")

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            expect("mimetype" in names, "缺 mimetype：{}".format(names))
            expect(any(name.endswith("META-INF/container.xml") for name in names),
                   "缺 container.xml：{}".format(names))

            # ebooklib 会把内容放在 EPUB/ 前缀下，所以按 basename 判断
            chapter_paths = sorted(name for name in names
                                   if os.path.basename(name).startswith("chapter_"))
            expect(len(chapter_paths) == 2, "章节数不对：{}".format(chapter_paths))
            for name in chapter_paths:
                expect(re.fullmatch(r"chapter_\d+\.xhtml", os.path.basename(name)),
                       "包内章节文件名必须是 chapter_N.xhtml，不能用标题：{}".format(name))
            expect(not any("第" in os.path.basename(name) for name in names),
                   "包内文件名不该出现中文标题（会破坏 EPUB 打包）：{}".format(names))

            image_paths = [name for name in names
                           if os.path.basename(name) == "001-01.jpg"]
            expect(len(image_paths) == 1, "图片应以 images/001-01.jpg 进包：{}".format(names))
            # 章节里写的是相对引用 images/001-01.jpg，所以图片必须落在
            # <章节所在目录>/images/ 下，否则阅读器加载不到
            chapter_dir = os.path.dirname(chapter_paths[0])
            expect(os.path.dirname(image_paths[0]).replace("\\", "/")
                   == "{}/images".format(chapter_dir),
                   "图片应在 {}/images/ 下才对得上相对引用：{}".format(chapter_dir, names))

            first = zf.read(chapter_paths[0]).decode("utf-8")
            second = zf.read(chapter_paths[1]).decode("utf-8")
            # ↓ 这两条是「epub 生成成功但每章 0 字节」那个坑的回归哨兵。
            #   当时 EpubHtml.content 被塞了完整 XHTML 文档，ebooklib 静默返回空字节，
            #   只看「文件存在」是发现不了的，必须逐章读回内容比对。
            expect(len(first) > 100 and len(second) > 100,
                   "章节内容不能为空（长度 {} / {}）".format(len(first), len(second)))
            expect("正文一" in first and "正文二" not in first, "第一章内容不对")
            expect("正文二" in second and "正文一" not in second, "第二章内容不对")
            expect("images/001-01.jpg" in first, "图片相对引用应保留")
            expect("第 1 章（上） #引用 [重要]" in first, "章节标题（显示用）应保留在文档里")

            opf_name = next(name for name in names if name.endswith("content.opf"))
            opf = zf.read(opf_name).decode("utf-8")
            expect("chapter_1.xhtml" in opf and "chapter_2.xhtml" in opf,
                   "两章都应在 spine 里")

    try:
        epub_export.export_epub([], os.path.join(tempfile.mkdtemp(), "x.epub"), title="空")
        raise AssertionError("没有章节时应报错")
    except LofterError:
        pass
    return "XHTML 规范化 / chapter_N.xhtml 命名 / 图片进包 / 结构可校验"


@check("浏览器读取登录信息：可选依赖与可操作报错")
def t_browser_cookie():
    from lofter import browser_cookie
    from lofter.errors import LofterError

    expect(browser_cookie.label_of("edge") == "Edge", "浏览器名映射错误")
    expect(browser_cookie.label_of(None) == "自动（所有浏览器）", "默认值映射错误")
    expect(browser_cookie.PIP_HINT.startswith("pip install"), "安装提示要能直接复制执行")

    usable, reason = browser_cookie.available()
    if not usable:
        try:
            browser_cookie.read_lofter_auth("chrome")
            raise AssertionError("依赖缺失时应报错并给出安装命令")
        except LofterError as exc:
            expect(browser_cookie.PIP_HINT in str(exc), "报错里应包含安装命令")
        return "跳过真读（未安装 browser-cookie3：{}）".format(reason)

    try:
        browser_cookie.read_lofter_auth("这个浏览器不存在")
        raise AssertionError("未知浏览器名应报错")
    except LofterError as exc:
        expect("不支持" in str(exc), "应提示可选值：{}".format(exc))

    # 本机浏览器不一定登录过 lofter，所以只断言「失败时给的是可操作的中文提示」
    try:
        auth = browser_cookie.read_lofter_auth("chrome")
        expect(bool(auth), "读到的 auth 不该为空")
        detail = "本机 Chrome 已登录，实际读到 {} 位".format(len(auth))
    except LofterError as exc:
        message = str(exc)
        expect(any(word in message for word in ("浏览器", "lofter", "cookie")),
               "报错应给出排查方向：{}".format(message))
        detail = "本机未登录，报错可读"
    return "依赖探测 / 未知浏览器拦截 / {}".format(detail)


@check("合集单个文件模式：只出一个正文文件 + 内联目录 + 整本级断点 + 切回多文件")
def t_collection_single_file():
    from lofter.config import Account, CollectionConfig
    from lofter.reporter import NullReporter
    from lofter.tasks import collection as collection_module
    from lofter.tasks import get_task
    from lofter.tasks.base import TaskContext

    class _FakeApi:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def collection_meta(self, collection_id):
            return {"id": collection_id, "name": "测试合集", "post_count": 3, "blog_id": "1",
                    "author": "作者甲", "tags": ["连载"], "description": "一个用于测试的合集"}

        def page_count(self, post_count, page_size):
            return 1

        def iter_collection_pages(self, collection_id, **kwargs):
            yield PROBE_COLLECTION_ITEMS

    # ---- 配置层面的映射（默认行为必须与旧版一致）
    default = CollectionConfig()
    expect(default.save_layout == "按章节分文件", "默认必须仍是按章节分文件")
    expect(not default.single_file, "默认不是单个文件模式")
    expect(default.write_chapter_files, "默认应出逐章文件")
    expect(not default.write_merged_file, "默认不该出合并文件")
    single = CollectionConfig(save_layout="单个文件")
    expect(single.single_file and not single.write_chapter_files, "单个文件模式映射错误")
    expect(single.write_merged_file, "单个文件模式必然要出那一个文件")
    both = CollectionConfig(merge_into_one=True)
    expect(both.write_chapter_files and both.write_merged_file, "逐章 + 完整版应两者都有")

    original = collection_module.AppApi
    collection_module.AppApi = _FakeApi
    try:
        with tempfile.TemporaryDirectory() as tmp:
            account = Account()
            account.login_auth = "probe"

            def run(cfg):
                ctx = TaskContext(account, NullReporter(), tmp)
                return get_task("collection").run(ctx, cfg)

            # ---- 单个文件模式
            cfg = CollectionConfig(source="collection_id", collection_id="777", base_dir=tmp,
                                   save_layout="单个文件", save_img=False)
            result = run(cfg)
            collection_dir = os.path.join(tmp, "collection", "测试合集")
            files = sorted(os.listdir(collection_dir))
            expect(files == ["测试合集 完整版.txt"],
                   "单个文件模式只该有一个正文文件（也没有目录/json）：{}".format(files))
            expect(result.stats.get("已保存章节") == 3, "应保存 3 章：{}".format(result.stats))

            single_path = os.path.join(collection_dir, "测试合集 完整版.txt")
            with open(single_path, encoding="utf-8") as fp:
                text = fp.read()
            expect("测试合集" in text and "作者：作者甲" in text, "缺合集头信息")
            expect("章节数：3" in text, "缺章节数")
            expect("tag：连载" in text, "缺 tag")
            expect("一个用于测试的合集" in text, "缺合集简介")
            expect("目录：" in text, "缺内联目录")
            for title in ("第一章 初遇", "第二章 再会", "第三章 归途"):
                expect(title in text, "正文里应出现 {}".format(title))
            expect("第 1 章 第一章 初遇" in text, "章节标题应是「第 N 章 标题」形态")
            expect("第 3 章 第三章 归途" in text, "第三章标题不对")
            expect("正文一" in text and "正文三" in text, "正文不全")
            expect("合集：" not in text, "单文件里不该再有「合集：…（第 N / M 章）」行")
            expect("上一篇" not in text and "下一篇" not in text,
                   "单文件里不该有跨文件的上下篇导航")
            expect(text.count("第 1 章") == 1 and text.count("第 3 章") == 1,
                   "每章标题只该出现一次")

            # ---- 断点粒度是「整本」
            result2 = run(cfg)
            expect(result2.stats.get("整本跳过") == 1,
                   "第二次应整本跳过：{}".format(result2.stats))
            expect(result2.stats.get("已保存章节") is None, "整本跳过时不该再保存章节")

            # ---- 文件名跟随「合并版文件名」
            cfg_renamed = CollectionConfig(source="collection_id", collection_id="777",
                                           base_dir=tmp, save_layout="单个文件",
                                           save_img=False, merge_filename="全本")
            run(cfg_renamed)
            expect(os.path.exists(os.path.join(collection_dir, "测试合集 全本.txt")),
                   "文件名应跟随「合并版文件名」：{}".format(sorted(os.listdir(collection_dir))))

            # ---- 切回多文件模式：逐章文件 + 完整版共存，且不删用户已有文件
            cfg_multi = CollectionConfig(source="collection_id", collection_id="777",
                                         base_dir=tmp, save_img=False, merge_into_one=True)
            result3 = run(cfg_multi)
            files3 = sorted(os.listdir(collection_dir))
            expect("001_第一章 初遇.txt" in files3 and "003_第三章 归途.txt" in files3,
                   "切回多文件模式应产出逐章文件：{}".format(files3))
            expect("测试合集 完整版.txt" in files3, "应同时产出完整版")
            expect("测试合集 全本.txt" in files3, "切换模式不该删掉用户已有的文件")
            expect(result3.stats.get("已保存章节") == 3, "首次多文件模式应保存 3 章")
            chapter_one = os.path.join(collection_dir, "001_第一章 初遇.txt")
            with open(chapter_one, encoding="utf-8") as fp:
                chapter_text = fp.read()
            expect("1. 第一章 初遇" in chapter_text,
                   "逐章文件应保持「独立文件」形态（1. 标题）：{}".format(chapter_text[:80]))
            expect("合集：测试合集（第 1 / 3 章）" in chapter_text, "逐章文件应带合集行")
            expect("下一篇" in chapter_text, "逐章文件应有跨文件导航")

            # ---- 多文件模式下的逐章断点仍然有效
            result4 = run(cfg_multi)
            expect(result4.stats.get("已存在跳过") == 3,
                   "多文件模式下第二次应逐章跳过：{}".format(result4.stats))
    finally:
        collection_module.AppApi = original
    return "只出一个文件 / 内联目录 / 整本断点 / 改名生效 / 切模式不删旧文件 / 两套章节形态"


# ====================================================================== main
def main() -> int:
    parser = argparse.ArgumentParser(description="lofterSpider 离线冒烟测试")
    parser.add_argument("--no-gui", action="store_true",
                        help="跳过 GUI 用例（无显示环境时用）")
    parser.parse_args()

    # 用例已由装饰器执行完毕（见文件中的定义顺序）
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed

    width = max(len(name) for name, _, _ in RESULTS) + 2
    print("\n" + "=" * (width + 58))
    print("lofterSpider 冒烟测试（离线，无需登录）")
    print("Python {} @ {}".format(sys.version.split()[0], sys.executable))
    print("=" * (width + 58))
    for name, ok, detail in RESULTS:
        print("{} {:<{w}} {}".format("✓" if ok else "✗", name, detail, w=width))
    print("-" * (width + 58))
    print("通过 {}/{}，失败 {}".format(passed, len(RESULTS), failed))
    print("=" * (width + 58) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
