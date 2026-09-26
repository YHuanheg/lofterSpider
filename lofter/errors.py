"""领域异常。

原脚本用 ``print(...)`` + ``exit()`` 表达「可预期的失败」，这在 GUI 里会直接
杀掉进程且没人看得到原因。这里统一改为异常：

* CLI 捕获后打印并返回非 0 退出码；
* GUI 捕获后在日志区红字输出并弹窗；
* 任务内部只负责 ``raise``，不负责决定怎么展示。
"""

from __future__ import annotations

__all__ = [
    "LofterError",
    "ConfigError",
    "AuthError",
    "NetworkError",
    "ParseError",
    "TaskCancelled",
    "NeedUserInput",
]


class LofterError(Exception):
    """本项目所有自定义异常的基类。"""


class ConfigError(LofterError):
    """配置缺失或非法（url 没填、模式拼错、日期格式不对等）。"""


class AuthError(LofterError):
    """登录信息无效：login_auth 没填、cookie 过期、login_key 与登录方式不匹配。"""


class NetworkError(LofterError):
    """HTTP 层失败：超时、连接错误、非 2xx 状态码。"""


class ParseError(LofterError):
    """页面/接口结构变化，预期字段解析不出来。"""


class TaskCancelled(LofterError):
    """用户主动取消，不算错误。"""


class NeedUserInput(LofterError):
    """任务需要用户确认但当前 Reporter 不支持交互。

    CLI 与 GUI 都实现了交互式 ``Reporter``，只有 ``NullReporter`` 会走到这里，
    用于测试环境避免任务卡在等输入上。
    """
