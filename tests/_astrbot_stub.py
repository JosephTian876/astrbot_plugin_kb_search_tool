"""最小 ``astrbot`` 替身：只提供本插件真正用到的**公开面**。

为什么自造替身而不 ``import astrbot``：

1. ``astrbot.core.__init__`` 在 **import 期**就 ``os.makedirs(get_astrbot_data_path())``
   并构造 ``AstrBotConfig()``；而 root 默认取当前工作目录 → 会在仓库根凭空写出一个
   含密钥的 ``data/cmd_config.json``。测试必须能在干净仓库里跑。
2. 替身里**刻意没有** ``astrbot.core`` 子模块：插件一旦退化回
   ``from astrbot.core...`` 私有导入，测试会在收集阶段直接 ImportError 失败。
   这就是"只依赖 ``astrbot.api`` 公开面"这条约束的回归钉。

替身只实现本插件用到的成员，多一个都不给 —— 这样测试不会意外依赖核心没有的 API。
"""

from __future__ import annotations

import logging
import sys
import types

__all__ = [
    "CAPTURED",
    "FILTER",
    "SP",
    "AstrMessageEvent",
    "FunctionTool",
    "ToolSet",
    "install",
]


class _Collector(logging.Handler):
    """把日志记录收集到 ``CAPTURED``，让测试能断言"说了什么、说几次、什么级别"。"""

    def emit(self, record: logging.LogRecord) -> None:
        CAPTURED.append(record)


#: 替身 logger 的输出。测试在每个用例开始前清空（见 conftest 的 autouse fixture）。
CAPTURED: list[logging.LogRecord] = []

LOGGER = logging.getLogger("kbtool-test")
LOGGER.setLevel(logging.DEBUG)  # DEBUG 也要收：注入成功只在 DEBUG 留痕
LOGGER.propagate = False
LOGGER.addHandler(_Collector())


class FunctionTool:
    """``astrbot.core.agent.tool.FunctionTool`` 的最小替身。

    插件只用到 ``name``（判重、日志）与实例身份（"注入的是核心缓存的那一个"）；
    ``active`` 只被替身的 ``ToolSet.add_tool`` 用来对齐核心的"优先 active"语义。
    """

    def __init__(self, name: str = "tool", *, active: bool = True) -> None:
        self.name = name
        self.description = ""
        self.active = active

    def __repr__(self) -> str:  # 断言失败时的可读输出
        return f"FunctionTool(name={self.name})"


class ToolSet:
    """``astrbot.core.agent.tool.ToolSet`` 的最小替身。

    只实现插件用到的 ``tools`` / ``add_tool`` / ``get_tool``，但 ``add_tool`` 的语义与
    核心**逐条对齐**（``core/agent/tool.py:91-108``）：

    - 同名且新工具 ``active`` → 覆盖旧的；
    - 同名且新工具不 active、旧的 active → **保留旧的**；
    - 同名且两者都不 active → 覆盖（核心的 ``not existing_active`` 分支）；
    - 无同名 → 追加到末尾。

    为什么要在替身里也模拟这条："同名覆盖"是核心的**真实**语义，替身若只做最宽松的
    "有同名就覆盖"，就等于把契约说简单了 —— 而插件真正的保证来自它自己的 ``get_tool``
    判重（``test_07``），不是替身。替身与核心一致，测试才有资格当契约钉子。
    """

    def __init__(self) -> None:
        self.tools: list[FunctionTool] = []

    def add_tool(self, tool: FunctionTool) -> None:
        for index, existing in enumerate(self.tools):
            if existing.name == tool.name:
                existing_active = bool(getattr(existing, "active", True))
                new_active = bool(getattr(tool, "active", True))
                if new_active or not existing_active:
                    self.tools[index] = tool
                return
        self.tools.append(tool)

    def get_tool(self, name: str) -> FunctionTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class _SharedPreferences:
    """``astrbot.api.sp`` 的最小替身（会话级偏好设置，内存字典）。"""

    def __init__(self) -> None:
        #: ``{(umo, key): value}``；缺省时 ``session_get`` 返回 ``default``（与核心一致）。
        self.session: dict[tuple[str, str], object] = {}
        #: 设置后 ``session_get`` 直接抛错，用于验证插件的 fail-open 姿态。
        self.error: Exception | None = None

    async def session_get(self, umo: str, key: str, default: object = None):
        if self.error is not None:
            raise self.error
        return self.session.get((umo, key), default)


#: 替身的 ``astrbot.api.sp`` 单例；所有用例共享，每个用例前由 conftest 重置。
SP = _SharedPreferences()


class ProviderRequest:
    """``astrbot.core.provider.entities.ProviderRequest`` 的最小替身。

    插件只读写 ``func_tool`` 这一个字段；``prompt`` / ``contexts`` 等字段插件不碰，
    替身就不提供 —— 与其它替身一样，只给插件真正用到的成员。
    """

    def __init__(self, func_tool: ToolSet | None = None) -> None:
        self.func_tool = func_tool


class AstrMessageEvent:
    """``AstrMessageEvent`` 的最小替身：插件只读 ``unified_msg_origin``。"""

    def __init__(
        self, unified_msg_origin: str = "aiocqhttp:FriendMessage:10086"
    ) -> None:
        self.unified_msg_origin = unified_msg_origin


class _Filter:
    """``astrbot.api.event.filter`` 的最小替身。"""

    def __init__(self) -> None:
        #: 每次钩子注册时收到的**完整 kwargs**（钉住关键字名，而不只是它的值）。
        self.on_llm_request_kwargs: list[dict[str, object]] = []

    def on_llm_request(self, **kwargs):
        self.on_llm_request_kwargs.append(kwargs)

        def decorator(func):
            return func

        return decorator


FILTER = _Filter()


class Star:
    """``astrbot.api.star.Star`` 的最小替身：真身只做 ``self.context = context`` 等初始化。"""

    def __init__(self, context, config: dict | None = None) -> None:
        self.context = context


class Context:
    """``astrbot.api.star.Context`` 的占位基类（插件只把它当类型标注用）。"""


def install() -> None:
    """把替身注册进 ``sys.modules``；必须在导入插件模块**之前**调用，重复调用无害。"""
    if getattr(sys.modules.get("astrbot"), "__kbtool_stub__", False):
        return

    api = types.ModuleType("astrbot.api")
    api.__all__ = ["FunctionTool", "ToolSet", "logger", "sp"]
    api.FunctionTool = FunctionTool
    api.ToolSet = ToolSet
    api.logger = LOGGER
    api.sp = SP
    api.__path__ = []  # 让 `astrbot.api.xxx` 的导入按包处理

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = FILTER

    provider = types.ModuleType("astrbot.api.provider")
    provider.__all__ = ["ProviderRequest"]
    provider.ProviderRequest = ProviderRequest

    star = types.ModuleType("astrbot.api.star")
    star.Context = Context
    star.Star = Star

    pkg = types.ModuleType("astrbot")
    pkg.__kbtool_stub__ = True
    pkg.__path__ = []
    pkg.api = api

    api.event = event
    api.provider = provider
    api.star = star

    sys.modules.update(
        {
            "astrbot": pkg,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star,
        }
    )
