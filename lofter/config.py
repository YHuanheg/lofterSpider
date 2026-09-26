"""任务配置：声明式字段定义 + JSON 持久化 + 从 ``login_info.py`` 自动迁移。

设计要点
--------
原脚本的参数都写在各文件 ``if __name__ == "__main__":`` 里，改参数＝改源码。
这里把每个任务的参数抽成 dataclass，并用 :class:`Field` **声明**每个字段的
标签/类型/可选值/帮助文本。于是：

* GUI 按 ``FIELDS`` 自动生成表单，新增任务不用写界面代码；
* CLI 按 ``FIELDS`` 自动生成 ``--参数``，与 GUI 共用一套默认值；
* 配置落盘成 ``config/settings.json``，可备份、可分享（不含登录凭证）。

登录凭证单独放 ``config/account.json``（已加入 .gitignore）。
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import (MISSING, asdict, dataclass, field as dc_field,
                         fields as dc_fields, is_dataclass)
from pathlib import Path
from typing import Any, Optional, Sequence

from .errors import ConfigError

__all__ = [
    "PROJECT_ROOT",
    "CONFIG_DIR",
    "ACCOUNT_FILE",
    "SETTINGS_FILE",
    "Field",
    "Account",
    "BaseConfig",
    "LikeShareTagConfig",
    "AuthorImgConfig",
    "AuthorTxtConfig",
    "BlogsConfig",
    "HomepageConfig",
    "PhoneTagConfig",
    "CONFIG_CLASSES",
    "load_account",
    "save_account",
    "load_task_configs",
    "save_task_configs",
    "load_app_state",
    "save_app_state",
    "resolve_base_dir",
    "migrate_account_from_login_info",
    "coerce_value",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
ACCOUNT_FILE = CONFIG_DIR / "account.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

ACCOUNT_JSON_KEYS = {"login_key", "login_auth"}

# 登录方式 → cookie 名（来自 README 的对应表）
LOGIN_KEY_CHOICES = (
    "LOFTER-PHONE-LOGIN-AUTH",
    "Authorization",
    "LOFTER_SESS",
    "NTES_SESS",
)

# 布尔值的中英文表示。
# **不要用 ``bool(value)`` 判布尔**：``bool("false")`` 恒为 True，
# 手改 config/settings.json 把开关写成 "false" 时会被误判成开启。
# 这里用白名单，认不出来的一律当 False。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t", "是", "真", "开", "启动", "启用"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off", "n", "f", "否", "假", "关", "关闭", "停用"})


def to_bool(value: Any) -> bool:
    """把各种表示法转成 bool，认不出来的当 False。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return False


# --------------------------------------------------------------------- 字段声明
@dataclass(frozen=True)
class Field:
    """一个可配置项的描述。"""

    name: str
    label: str
    kind: str = "str"          # str|int|bool|choice|list|text|json|path|secret
    default: Any = ""
    choices: Sequence[str] = ()
    help: str = ""
    group: str = "基础设置"

    def coerce(self, raw: Any) -> Any:
        return coerce_value(self, raw)


def coerce_value(spec: Field, raw: Any) -> Any:
    """把界面/命令行传来的字符串转成配置需要的类型。"""
    if spec.kind == "int":
        if isinstance(raw, bool):
            return int(raw)
        text = str(raw).strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError as exc:
            raise ConfigError("「{}」需要一个整数，收到 {!r}".format(spec.label, raw)) from exc
    if spec.kind == "bool":
        return to_bool(raw)
    if spec.kind == "list":
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [line.strip() for line in str(raw).splitlines() if line.strip()]
    if spec.kind == "json":
        if isinstance(raw, (dict, list)):
            return raw
        text = str(raw).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError("「{}」需要合法 JSON：{}".format(spec.label, exc)) from exc
    if spec.kind == "choice":
        text = str(raw).strip()
        if text not in spec.choices:
            raise ConfigError("「{}」只能是 {} 之一，收到 {!r}".format(
                spec.label, " / ".join(spec.choices), raw))
        return text
    text = "" if raw is None else str(raw)
    return text.strip() if spec.kind in ("str", "path", "secret") else text


# --------------------------------------------------------------------- 配置基类
class ConfigBase:
    """配置基类：提供 ``to_dict`` / ``from_dict`` / ``set`` / ``get``。"""

    TITLE: str = ""
    FIELDS: tuple[Field, ...] = ()

    # ---- 序列化
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ConfigBase":
        data = data or {}
        valid = {f.name: f for f in dc_fields(cls)}
        kwargs = {}
        for key, value in data.items():
            if key not in valid:
                continue  # 忽略旧版本/未知字段，保证向后兼容
            spec = cls.field(key)
            try:
                kwargs[key] = spec.coerce(value) if spec else value
            except ConfigError:
                # 非法值退回默认值。注意 list/dict 字段的 default 是 MISSING，
                # 这种字段直接不传，交给 default_factory。
                if valid[key].default is not MISSING:
                    kwargs[key] = valid[key].default
        return cls(**kwargs)

    # ---- 字段访问
    @classmethod
    def field(cls, name: str) -> Optional[Field]:
        for spec in cls.FIELDS:
            if spec.name == name:
                return spec
        return None

    @classmethod
    def fields_in_group(cls, group: str) -> list[Field]:
        return [f for f in cls.FIELDS if f.group == group]

    @classmethod
    def groups(cls) -> list[str]:
        seen: list[str] = []
        for spec in cls.FIELDS:
            if spec.group not in seen:
                seen.append(spec.group)
        return seen

    def get(self, name: str) -> Any:
        return getattr(self, name)

    def set(self, name: str, raw: Any) -> None:
        spec = self.field(name)
        if spec is None:
            raise ConfigError("未知配置项 {}".format(name))
        setattr(self, name, spec.coerce(raw))


# --------------------------------------------------------------------- 各任务配置
@dataclass
class Account(ConfigBase):
    """登录信息。原 ``login_info.py``。"""

    TITLE = "登录信息"
    FIELDS = (
        Field("login_key", "登录方式对应的 cookie 名", "choice",
              default="LOFTER-PHONE-LOGIN-AUTH", choices=LOGIN_KEY_CHOICES,
              help="手机号登录=L OFTER-PHONE-LOGIN-AUTH；lofter id 登录=Authorization；"
                   "QQ/微信/微博登录=LOFTER_SESS；邮箱登录=NTES_SESS",
              group="账号"),
        Field("login_auth", "login_auth（cookie 值）", "secret", default="",
              help="浏览器 F12 → Application → Cookies → lofter.com → 复制对应字段的值。"
                   "README『填写登录信息』一节有图文步骤。",
              group="账号"),
    )

    login_key: str = "LOFTER-PHONE-LOGIN-AUTH"
    login_auth: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.login_auth.strip())

    def cookie_dict(self) -> dict:
        return {self.login_key: self.login_auth}

    def check(self) -> None:
        if not self.ready:
            from .errors import AuthError

            raise AuthError("还没有填 login_auth，去「登录信息」页填一下（README 有获取步骤）。")


@dataclass
class BaseConfig(ConfigBase):
    """所有抓取任务共有的两个设置。"""

    base_dir: str = "./dir"
    print_level: int = 0

    COMMON_FIELDS = (
        Field("base_dir", "输出 / 进度目录", "path", default="./dir",
              help="所有抓到的文件、以及断点用的 json 都在这里。原来叫 file_path，默认 ./dir",
              group="输出"),
        Field("print_level", "详细日志等级", "int", default=0,
              help="0=正常；1=把每条博客的解析结果都打出来，调试用", group="输出"),
    )


@dataclass
class LikeShareTagConfig(BaseConfig):
    """l13：保存我的喜欢 / 我的推荐 / 某个 tag 下的内容。"""

    TITLE = "保存喜欢 / 推荐 / tag（PC 端）"
    FIELDS = (
        Field("mode", "运行模式", "choice", default="tag",
              choices=("like1", "like2", "share", "tag"),
              help="like1=我的喜欢；like2=我的喜欢（可按开始时间过滤）；"
                   "share=我的推荐；tag=某个 tag 下的内容",
              group="基础设置"),
        Field("url", "链接", "str", default="",
              help="like1/like2/share 填个人主页（去掉 ? 后面的参数）；"
                   "tag 填 tag 链接，结尾是 new/total/month/week/date 之一",
              group="基础设置"),
        Field("save_article", "保存文章（有标题的文字）", "bool", default=True, group="保存哪几种"),
        Field("save_text", "保存文本（无标题的文字）", "bool", default=True, group="保存哪几种"),
        Field("save_long_article", "保存长文章", "bool", default=True, group="保存哪几种"),
        Field("save_img", "保存图片博客", "bool", default=True,
              help="图片量大时最慢，平均 1000 条要 20-40 分钟", group="保存哪几种"),
        Field("classify_by_tag", "按 tag 分类到不同文件夹", "bool", default=False,
              help="按作者打的第一个 tag 建子目录。开了好整理", group="自动整理"),
        Field("prior_tags", "优先 tag（一行一个）", "list", default=None,
              help="留空则不启用。非空时，命中优先 tag 的博客进 prior/<tag>，"
                   "其余看下一个选项", group="自动整理"),
        Field("agg_non_prior_tag", "非优先 tag 聚合到 other", "bool", default=False,
              help="只在启用优先 tag 时有效：命中不了优先 tag 的全部丢进 other/",
              group="自动整理"),
        Field("start_time", "起始时间（仅 like2）", "str", default="",
              help="格式 2020-02-01。用来增量抓「上次运行之后新点的喜欢」",
              group="过滤"),
        Field("min_hot", "最低热度（仅 tag 模式）", "int", default=0,
              help="热度低于该值的博客直接跳过", group="过滤"),
        Field("save_img_in_text", "同时保存文章/文本里插的图片", "bool", default=True,
              help="lofter 允许在正文里插外链图，外链图有保存失败的可能", group="输出"),
        Field("tag_filt_num", "tag 统计只显示出现次数 >", "int", default=50,
              help="运行中会统计所有 tag 的频次，只打印超过这个阈值的", group="输出"),
        Field("pause_before_save", "保存前暂停让我确认自动整理选项", "bool", default=False,
              help="原脚本在这里强制要求输入 ok 才继续；改成开关后可以无人值守跑",
              group="输出"),
        Field("reset_after_save", "跑完后自动清空进度文件（方便下次重新全量抓）", "bool",
              default=False,
              help="原脚本跑完会问你 yes/no。注意：清了进度下次会重新抓全部内容",
              group="输出"),
    ) + BaseConfig.COMMON_FIELDS

    mode: str = "tag"
    url: str = ""
    save_article: bool = True
    save_text: bool = True
    save_long_article: bool = True
    save_img: bool = True
    classify_by_tag: bool = False
    prior_tags: list = dc_field(default_factory=list)
    agg_non_prior_tag: bool = False
    start_time: str = ""
    min_hot: int = 0
    save_img_in_text: bool = True
    tag_filt_num: int = 50
    pause_before_save: bool = False
    reset_after_save: bool = False

    @property
    def save_mode(self) -> dict:
        """兼容原脚本内部的 ``save_mode`` 字典。"""
        return {
            "article": int(bool(self.save_article)),
            "text": int(bool(self.save_text)),
            "long article": int(bool(self.save_long_article)),
            "img": int(bool(self.save_img)),
        }

    @property
    def auto_sort_setting(self) -> dict:
        """写进 img_save_info.json 的「自动整理设置」，用来检测设置变更。"""
        return {
            "按tag分类": int(bool(self.classify_by_tag)),
            "优先tag": list(self.prior_tags),
            "非优先tag聚合": int(bool(self.agg_non_prior_tag)),
        }

    def validate(self) -> None:
        if self.mode in ("like1", "like2", "share", "tag") and not self.url.strip():
            raise ConfigError("{} 模式必须填链接".format(self.mode))
        if self.start_time and self.mode != "like2":
            # 原脚本里这个字段本身也只对 like2 生效，这里只提示不报错
            pass


@dataclass
class AuthorImgConfig(BaseConfig):
    """l4：保存某个作者主页的所有图片。"""

    TITLE = "保存作者主页的图片（PC 端，走归档页）"
    FIELDS = (
        Field("author_url", "作者主页链接", "str", default="https://",
              help="形如 https://xxx.lofter.com/ ，**结尾的斜杠不能少**",
              group="基础设置"),
        Field("start_time", "起始时间", "str", default="", help="yyyy-MM-dd，留空不限制",
              group="过滤"),
        Field("end_time", "结束时间", "str", default="", help="yyyy-MM-dd，留空不限制",
              group="过滤"),
        Field("target_tags", "只保留这些 tag（一行一个）", "list", default=None,
              help="留空=不过滤。注意部分作者把 tag 放在头像下方会导致过滤失效",
              group="过滤"),
        Field("tags_filter_mode", "tag 过滤模式", "choice", default="in",
              choices=("in", "out"),
              help="in=没有 tag 的博客也保留；out=没有 tag 的博客丢弃",
              group="过滤"),
        Field("file_update_interval", "每多少条刷新一次进度文件", "int", default=10,
              help="越小越安全但越慢", group="输出"),
    ) + BaseConfig.COMMON_FIELDS

    author_url: str = "https://"
    start_time: str = ""
    end_time: str = ""
    target_tags: list = dc_field(default_factory=list)
    tags_filter_mode: str = "in"
    file_update_interval: int = 10

    def validate(self) -> None:
        if not self.author_url.startswith("http") or self.author_url.endswith("://"):
            raise ConfigError("请填写完整的作者主页链接，形如 https://xxx.lofter.com/")
        if not self.author_url.endswith("/"):
            raise ConfigError("作者主页链接结尾必须带 /")


@dataclass
class AuthorTxtConfig(BaseConfig):
    """l9：保存某个作者主页的所有文章 / 文本。"""

    TITLE = "保存作者主页的文章与文本（PC 端，走归档页）"
    FIELDS = (
        Field("author_url", "作者主页链接", "str", default="https://",
              help="形如 https://xxx.lofter.com/ ，**结尾的斜杠不能少**",
              group="基础设置"),
        Field("target_tags", "只保留这些 tag（一行一个）", "list", default=None,
              help="留空=不过滤", group="过滤"),
        Field("tags_filter_mode", "tag 过滤模式", "choice", default="in",
              choices=("in", "out"),
              help="in=没有 tag 的博客也保留；out=没有 tag 的博客丢弃", group="过滤"),
        Field("get_comm", "同时抓评论", "bool", default=False,
              help="会明显变慢", group="内容"),
        Field("additional_break", "解析段之间的额外换行数", "int", default=0,
              help="1 就多一个换行，看阅读习惯", group="内容"),
        Field("start_time", "起始时间", "str", default="", help="yyyy-MM-dd，留空不限制",
              group="过滤"),
        Field("end_time", "结束时间", "str", default="", help="yyyy-MM-dd，留空不限制",
              group="过滤"),
        Field("chapter_merge_title", "章节合并：标题包含这些词就合并（一行一个）", "list",
              default=None, help="留空=不合并。合并后文件名用你写的这个词", group="章节合并"),
        Field("auto_chapter_merge_title", "自动识别 (上)(中)(下)(1)(2) 并合并", "bool",
              default=False, group="章节合并"),
        Field("additional_chapter_index", "合并后每章前加「第 n 章」", "bool", default=False,
              help="只是顺序编号，不识别原标题是第几章", group="章节合并"),
    ) + BaseConfig.COMMON_FIELDS

    author_url: str = "https://"
    target_tags: list = dc_field(default_factory=list)
    tags_filter_mode: str = "in"
    get_comm: bool = False
    additional_break: int = 0
    start_time: str = ""
    end_time: str = ""
    chapter_merge_title: list = dc_field(default_factory=list)
    auto_chapter_merge_title: bool = False
    additional_chapter_index: bool = False

    def validate(self) -> None:
        if not self.author_url.startswith("http") or self.author_url.endswith("://"):
            raise ConfigError("请填写完整的作者主页链接，形如 https://xxx.lofter.com/")
        if not self.author_url.endswith("/"):
            raise ConfigError("作者主页链接结尾必须带 /")


@dataclass
class BlogsConfig(BaseConfig):
    """l8 + l10 合并：按链接列表保存单篇博客（图片或文字）。"""

    TITLE = "单篇保存（按链接列表）"
    FIELDS = (
        Field("kind", "保存什么", "choice", default="img", choices=("img", "txt"),
              help="img=只存图片；txt=存正文（文章/文本）", group="基础设置"),
        Field("source", "链接来源", "choice", default="file", choices=("file", "custom"),
              help="file=读输出目录下的 img_list / txt_list；custom=用下面文本框里的链接",
              group="基础设置"),
        Field("urls", "链接列表（一行一个）", "list", default=None,
              help="source 选 custom 时生效。配合「主页扫描」任务可以拿到含仅自己可见的链接",
              group="基础设置"),
    ) + BaseConfig.COMMON_FIELDS

    kind: str = "img"
    source: str = "file"
    urls: list = dc_field(default_factory=list)

    @property
    def list_file_name(self) -> str:
        return "img_list" if self.kind == "img" else "txt_list"

    def validate(self) -> None:
        if self.source == "custom" and not self.urls:
            raise ConfigError("source 选了 custom，但链接列表是空的")


@dataclass
class HomepageConfig(BaseConfig):
    """l14：从主页翻页抠出所有博客链接（**能拿到仅自己可见的**，仅支持默认模板）。"""

    TITLE = "主页扫描（含仅自己可见，仅默认模板）"
    FIELDS = (
        Field("url", "主页链接", "str", default="https://",
              help="只能扫 lofter 默认模板（灰色良品）的主页，结尾带 /", group="基础设置"),
        Field("output", "结果输出到", "str", default="links.txt",
              help="一行一个博客链接，可直接粘到「单篇保存」的链接列表里", group="输出"),
    ) + BaseConfig.COMMON_FIELDS

    url: str = "https://"
    output: str = "links.txt"

    def validate(self) -> None:
        if not self.url.startswith("http") or self.url.endswith("://"):
            raise ConfigError("请填写完整的主页链接，形如 https://xxx.lofter.com/")


@dataclass
class AppApiConfig(BaseConfig):
    """走 ``api.lofter.com``（App 端接口）的任务共用的凭证与网络设置。

    合集下载与手机端 tag 抓取都要这一套，所以抽出来避免抄两份。
    """

    APP_FIELDS = (
        Field("headers_json", "额外请求头（JSON）", "json", default={},
              help="一般不用填。接口对某些账号要求额外的设备头时，在这里补",
              group="App 凭证"),
        Field("cookies_json", "App cookies（JSON）", "json", default={},
              help='形如 {"usertrack": "...", "NEWTOKEN": "..."}；一般不用填',
              group="App 凭证"),
        Field("verify_ssl", "校验 SSL 证书", "bool", default=True,
              help="报证书错误（SSL: CERTIFICATE_VERIFY_FAILED）时把它关掉再试",
              group="App 凭证"),
    )
    APP_COMMON_FIELDS = APP_FIELDS + BaseConfig.COMMON_FIELDS

    headers_json: dict = dc_field(default_factory=dict)
    cookies_json: dict = dc_field(default_factory=dict)
    verify_ssl: bool = True


@dataclass
class PhoneTagConfig(AppApiConfig):
    """l15：手机端 tag 接口（实验性，原脚本本身也没写完）。"""

    TITLE = "手机端 tag 抓取（实验性，未完成）"
    FIELDS = (
        Field("tag", "tag 名", "str", default="", help="例如：冬兵", group="基础设置"),
        Field("list_type", "榜单类型", "choice", default="total",
              choices=("total", "month", "week", "date"),
              help="total=总榜；month/week/date=月/周/日榜", group="基础设置"),
        Field("timelimit", "月份筛选", "str", default="",
              help="总榜可用，格式 yyyyMM 例 202404", group="基础设置"),
        Field("blog_type", "博客类型", "choice", default="",
              choices=("", "1", "2"), help="空=全都要；1=只要文字；2=只要图片",
              group="基础设置"),
    ) + AppApiConfig.APP_COMMON_FIELDS

    tag: str = ""
    list_type: str = "total"
    timelimit: str = ""
    blog_type: str = ""

    def validate(self) -> None:
        if not self.tag.strip():
            raise ConfigError("请填写 tag 名")


@dataclass
class CollectionConfig(AppApiConfig):
    """合集下载：把 lofter 合集（连载整本）按章节顺序抓下来。

    参考 ``Bueer99/Lofter_Passage_Get`` 想解决的问题（网页端看不到合集），
    但**不沿用它的实现**：那边靠 Selenium 点「上一篇」逐篇跳，本项目走
    App 端 ``postCollection.api``——一次请求就能拿到 50 篇的**完整正文 HTML**，
    不需要浏览器、不需要 ChromeDriver、也不需要逐篇再请求。
    """

    TITLE = "下载 lofter 合集（连载整本）"
    FIELDS = (
        Field("source", "合集从哪来", "choice", default="collection_id",
              choices=("collection_id", "article", "author", "subscription", "follow_links"),
              help="collection_id=直接给合集 ID（可一次给多个，最稳）；"
                   "article=给一篇合集内的文章链接，自动反查出所属合集；"
                   "author=给作者主页，下载他的全部合集；"
                   "subscription=下载我订阅的全部合集（要登录）；"
                   "follow_links=顺着文章页的「上一篇/下一篇」逐篇走（兜底，实验性）",
              group="基础设置"),
        Field("collection_id", "合集 ID（一行一个，可多个）", "list", default=None,
              help="App 里进合集 → 分享 → 复制链接，链接里 CollectionId 后面那串数字就是。"
                   "填多个会逐个下载，其中某个失败不影响其它合集",
              group="基础设置"),
        Field("url", "文章链接", "str", default="",
              help="source 选 article / follow_links 时填。带 ?collectionId= 的分享链接也能直接粘",
              group="基础设置"),
        Field("author_url", "作者主页 / 三级域名", "str", default="",
              help="source 选 author 时填，如 https://xxx.lofter.com/ ，或直接写 xxx",
              group="基础设置"),
        Field("chapter_order", "章节顺序", "choice", default="自动（接口顺序）",
              choices=("自动（接口顺序）", "正序（从旧到新）", "倒序（从新到旧）"),
              help="接口默认按作者排版顺序返回；发现章节乱了就改成按发布时间排序",
              group="基础设置"),
        Field("max_chapters", "每个合集最多抓几章", "int", default=0,
              help="0 = 全部。想先试水就填 3 或 5。批量时是**每个合集**各自计算的",
              group="基础设置"),

        Field("save_layout", "保存方式", "choice", default="按章节分文件",
              choices=("按章节分文件", "单个文件"),
              help="按章节分文件=001_第一章.txt、002_第二章.txt…（默认，与旧版一致）；"
                   "单个文件=全部章节拼进**一个**文件，合集目录下只出一个正文文件。"
                   "选「单个文件」时「生成合集目录文件」「保存原始 JSON」「另存一个合并的完整版」"
                   "都会被忽略（目录已内联进正文）",
              group="保存内容"),
        Field("save_format", "保存格式", "choice", default="txt", choices=("txt", "md"),
              help="txt=纯文本（与本项目其它任务一致）；md=Markdown，图片用 ![]() 引用",
              group="保存内容"),
        Field("save_img", "下载正文里的图片", "bool", default=True,
              help="存到合集目录下的 images/，正文里替换成本地相对路径", group="保存内容"),
        Field("img_workers", "图片并发下载数", "int", default=3,
              help="1 = 串行。调太高容易被图床限流，建议不超过 5", group="保存内容"),
        Field("export_epub", "额外导出一本 EPUB", "bool", default=False,
              help="需要额外装：pip install EbookLib。没装时这个开关会被跳过并给出提示",
              group="保存内容"),
        Field("include_tags", "在每篇里写上 tag", "bool", default=True, group="保存内容"),
        Field("add_nav", "文末加上一篇/下一篇链接", "bool", default=True, group="保存内容"),
        Field("save_index", "生成合集目录文件", "bool", default=True,
              help="章节列表 + 合集简介 + tag，方便快速翻阅", group="保存内容"),
        Field("save_json", "保存原始 JSON", "bool", default=True,
              help="合集全部章节的原始数据，便于二次处理（体积较大）", group="保存内容"),

        Field("title_filter", "只保存标题含这个词的章节", "str", default="",
              help="留空 = 全部保存", group="过滤"),
        Field("skip_subscription_invalid", "跳过已失效的订阅合集", "bool", default=True,
              help="只在 source=subscription 时有效；失效的仍会记进统计", group="过滤"),

        Field("skip_existing", "已下载的章节跳过", "bool", default=True,
              help="断点续传：中断后重跑只补缺的章节", group="输出"),
        Field("merge_into_one", "另存一个合并的完整版", "bool", default=False,
              help="把全部章节拼成一个文件，每章前加「第 N 章」", group="输出"),
        Field("merge_filename", "合并版文件名（不含后缀）", "str", default="完整版",
              help="留空则用「合集名 完整版」", group="输出"),
        Field("page_size", "每页抓多少章", "int", default=50,
              help="接口一次最多给 50，填大也没用", group="输出"),
        Field("max_retries", "网络失败重试次数", "int", default=3, group="输出"),
    ) + AppApiConfig.APP_COMMON_FIELDS

    source: str = "collection_id"
    collection_id: list = dc_field(default_factory=list)
    url: str = ""
    author_url: str = ""
    chapter_order: str = "自动（接口顺序）"
    max_chapters: int = 0
    save_layout: str = "按章节分文件"
    save_format: str = "txt"
    save_img: bool = True
    img_workers: int = 3
    export_epub: bool = False
    include_tags: bool = True
    add_nav: bool = True
    save_index: bool = True
    save_json: bool = True
    title_filter: str = ""
    skip_subscription_invalid: bool = True
    skip_existing: bool = True
    merge_into_one: bool = False
    merge_filename: str = "完整版"
    page_size: int = 50
    max_retries: int = 3

    @property
    def file_suffix(self) -> str:
        return ".md" if self.save_format == "md" else ".txt"

    @property
    def sort_mode(self) -> str:
        """把界面上的中文选项映射成内部排序模式。"""
        if self.chapter_order.startswith("正序"):
            return "asc"
        if self.chapter_order.startswith("倒序"):
            return "desc"
        return "api"

    @property
    def single_file(self) -> bool:
        """是否「只出一个正文文件」的保存方式。"""
        return str(self.save_layout).startswith("单个")

    @property
    def write_chapter_files(self) -> bool:
        """是否按章出文件；单个文件模式下不出。"""
        return not self.single_file

    @property
    def write_merged_file(self) -> bool:
        """是否出「合并形态」的文件。

        单个文件模式下它就是**唯一**的正文文件，必然要出；
        否则只看用户有没有勾「另存一个合并的完整版」。
        """
        return self.single_file or bool(self.merge_into_one)

    @property
    def collection_ids(self) -> list:
        """``collection_id`` 的规范化读取。

        它现在是**列表**字段（界面里一行一个），但要兼容两种老写法：
        早期版本的单个字符串、以及命令行里用逗号分隔的一串。所以这里同时按
        逗号、全角逗号、分号、空白切分。
        """
        raw = self.collection_id
        if isinstance(raw, str):
            chunks = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
            parts: list = []
            for chunk in chunks:
                parts.extend(chunk.split())
        else:
            parts = [str(item) for item in (raw or [])]
        cleaned = []
        for part in parts:
            text = part.strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @property
    def author_domain(self) -> str:
        """从作者主页链接里取出三级域名；已经是域名就原样返回。"""
        text = str(self.author_url).strip()
        if not text:
            return ""
        text = text.split("//")[-1].split("/")[0].strip()
        suffix = ".lofter.com"
        if text.endswith(suffix):
            text = text[: -len(suffix)]
        return text

    def validate(self) -> None:
        if self.source == "collection_id" and not self.collection_ids:
            raise ConfigError("「合集从哪来」选了合集 ID，但一个 ID 都没填")
        if self.source in ("article", "follow_links") and not str(self.url).strip():
            raise ConfigError("「合集从哪来」选了文章链接，但链接是空的")
        if self.source == "article" and not str(self.url).strip().startswith("http"):
            raise ConfigError("文章链接要以 http 开头")
        if self.source == "author" and not self.author_domain:
            raise ConfigError("「合集从哪来」选了作者主页，但作者主页 / 域名是空的")
        if self.page_size <= 0:
            raise ConfigError("每页抓多少章必须大于 0")
        if self.img_workers < 1:
            raise ConfigError("图片并发下载数必须大于等于 1")


CONFIG_CLASSES = {
    "collection": CollectionConfig,
    "like_share_tag": LikeShareTagConfig,
    "author_img": AuthorImgConfig,
    "author_txt": AuthorTxtConfig,
    "blogs": BlogsConfig,
    "homepage": HomepageConfig,
    "phone_tag": PhoneTagConfig,
}


# --------------------------------------------------------------------- 读写
def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_account() -> Account:
    """读登录信息；没有就尝试从老的 ``login_info.py`` 迁移一次。"""
    data = _read_json_file(ACCOUNT_FILE)
    if data:
        return Account.from_dict(data)
    migrated = migrate_account_from_login_info()
    if migrated is not None:
        save_account(migrated)
        return migrated
    return Account()


def save_account(account: Account) -> None:
    _ensure_config_dir()
    payload = {k: v for k, v in account.to_dict().items() if k in ACCOUNT_JSON_KEYS}
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    _restrict_permissions(ACCOUNT_FILE)


def _restrict_permissions(path: Path) -> None:
    """把只含自己可见的凭证文件收紧权限（Windows 上忽略失败）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_task_configs() -> dict:
    data = _read_json_file(SETTINGS_FILE)
    out = {}
    for tid, cls in CONFIG_CLASSES.items():
        out[tid] = cls.from_dict(data.get(tid))
    return out


def save_task_configs(configs: dict) -> None:
    _ensure_config_dir()
    data = _read_json_file(SETTINGS_FILE)
    for tid, cfg in configs.items():
        data[tid] = cfg.to_dict() if is_dataclass(cfg) else dict(cfg)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def load_app_state() -> dict:
    return dict(_read_json_file(SETTINGS_FILE).get("_app", {}))


def save_app_state(state: dict) -> None:
    _ensure_config_dir()
    data = _read_json_file(SETTINGS_FILE)
    data["_app"] = dict(state)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def resolve_base_dir(base_dir: str) -> str:
    """把相对输出目录解析到项目根，保证「双击运行」和「命令行运行」结果一致。

    原脚本用 ``./dir`` 相对当前工作目录，换个目录启动就找不到进度文件了。
    """
    text = (base_dir or "./dir").strip()
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path)


def migrate_account_from_login_info() -> Optional[Account]:
    """从旧版 ``login_info.py`` 里读出 ``login_key`` / ``login_auth``。

    用 ``ast`` 静态解析，**不 exec 用户的文件**。找不到就返回 ``None``。
    """
    candidates = [
        PROJECT_ROOT / "login_info.py",
        PROJECT_ROOT / "legacy" / "login_info.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        found: dict = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in ACCOUNT_JSON_KEYS:
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(value, str):
                        found[target.id] = value
        if found.get("login_auth"):
            return Account.from_dict(found)
    return None
