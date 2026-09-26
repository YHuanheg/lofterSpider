"""lofterSpider 重构后的核心包。

设计目标
--------
1. **无 GUI 依赖**：``lofter`` 包只依赖 requests / lxml / html2text，
   CLI 与 GUI 都只是它的一层壳，方便单独测试与复用。
2. **无阻塞式 IO**：原脚本里的 ``print`` / ``input`` / ``exit`` 全部换成
   :class:`lofter.reporter.Reporter` 回调与领域异常，这样才能被 GUI 驱动。
3. **配置外置**：任务参数从「源码里改常量」变成可序列化的 dataclass，
   GUI 直接按 ``FIELDS`` 声明式生成表单，CLI 也由同一份声明生成参数。
4. **行为对齐**：所有解析正则、文件名规则、分阶段断点语义都尽量与原脚本逐字对应，
   保证老用户已有的 ``dir/`` 进度文件仍可续跑。
"""

__version__ = "2.1.0"
__all__ = ["__version__"]
