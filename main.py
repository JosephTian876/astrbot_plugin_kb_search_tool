"""知识库检索常驻 —— 让 ``astr_kb_search`` 工具始终可用。

## 解决的问题

AstrBot 核心的 ``astr_main_agent._apply_kb()`` 是 **if/else 二选一**：

```python
if not config.kb_agentic_mode:
    if req.prompt is None or not req.prompt.strip():
        return                      # ← 纯附件消息（如只发视频）直接跳过
    ... 自动把 KB 结果注入 extra_user_content_parts
else:
    req.func_tool.add_tool(KnowledgeBaseQueryTool)   # ← 给工具，但不再自动注入
```

于是运营者面临两难：

- ``kb_agentic_mode=false``：有文字时自动注入 ✅，但**模型手上没有查知识库的工具** ❌
- ``kb_agentic_mode=true``：有工具 ✅，但**失去自动注入** ❌

## 本插件的做法

在 ``on_llm_request`` 阶段直接把 ``astr_kb_search`` 塞进 ``req.func_tool``，
**不碰 ``kb_agentic_mode``** —— 于是两者兼得：

- ✅ 自动注入照常工作（保持 ``kb_agentic_mode=false``）
- ✅ 模型始终有 ``astr_kb_search`` 可用

## 为什么这很重要（实测教训）

某部署的 persona 里写着「先查知识库再回答」，并把知识库描述成一个**文件夹树**
（``冬马和纱知识库_TE/``、``70_附录_边界速查`` …）却没给路径、也没给工具。
而 ``kb_agentic_mode=false`` 让 ``astr_kb_search`` 未注册 ——

**结果：模型把「查知识库」理解成「搜磁盘」**，用 ``astrbot_execute_python`` +
``os.walk`` 扫遍 ``C:\\`` 与 ``D:\\``，跑满 30 步工具调用、耗时 12 分钟才回复。
同一现象连续复现三次（04:23 / 05:04 / 06:22）。

有了本插件，persona 说「查知识库」时模型就有对应的工具可用，不必碰文件系统。
"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

try:
    from astrbot.core.agent.tool import ToolSet
except Exception:  # pragma: no cover - 版本差异兜底
    ToolSet = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_kb_search_tool"
PLUGIN_VERSION = "0.1.0"
PLUGIN_REPO = "https://github.com/JosephTian876/astrbot_plugin_kb_search_tool"

#: 核心工具名。核心在 agentic 模式下也注册同名工具，本插件复用同一个实例。
KB_TOOL_NAME = "astr_kb_search"

#: 比其他插件都晚执行，确保我们追加的工具不会被后续钩子覆盖掉。
HOOK_PRIORITY = -310000


@register(
    PLUGIN_NAME,
    "JosephTian876",
    "让 astr_kb_search 工具始终可用，同时保留知识库自动注入 —— 核心的 kb_agentic_mode 二选一做不到。",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class KBSearchToolPlugin(Star):
    """把内置的知识库检索工具常驻到每次 LLM 请求。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        raw = dict(config or {})
        self.enabled: bool = bool(raw.get("enable", True))
        self.only_when_kb_configured: bool = bool(
            raw.get("only_when_kb_configured", True)
        )
        self._warned: set[str] = set()

        # 启动自检：确认核心工具确实注册了
        self.tool_available = False
        try:
            self.context.get_llm_tool_manager().get_builtin_tool(KB_TOOL_NAME)
            self.tool_available = True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[KBTool] 无法取得内置工具 %s，插件不会生效: %s", KB_TOOL_NAME, exc
            )

        if self.tool_available:
            logger.info(
                "[KBTool] v%s 已加载 | 启用=%s | 工具=%s 已确认可用",
                PLUGIN_VERSION,
                self.enabled,
                KB_TOOL_NAME,
            )
        logger.info(
            "[KBTool] 提示：本插件让模型始终能调用 %s。"
            "可保持 kb_agentic_mode=false 以同时享受知识库自动注入。",
            KB_TOOL_NAME,
        )

    @filter.on_llm_request(priority=HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req: object) -> None:
        """确保本次请求的工具集中含有 astr_kb_search。"""
        try:
            await self._ensure_tool(req)
        except Exception as exc:  # noqa: BLE001
            # 绝不因为本插件打断正常对话
            logger.error("[KBTool] 注入工具时异常，已跳过: %s", exc, exc_info=True)

    async def _ensure_tool(self, req: object) -> None:
        if not self.enabled or not self.tool_available or ToolSet is None:
            return

        # 已有（核心 agentic 模式已加过，或前一个钩子加过）就不重复
        tool_set = getattr(req, "func_tool", None)
        if tool_set is not None and tool_set.get_tool(KB_TOOL_NAME) is not None:
            return

        if self.only_when_kb_configured and not self._kb_configured():
            key = "no-kb"
            if key not in self._warned:
                self._warned.add(key)
                logger.warning(
                    "[KBTool] 未配置任何知识库（kb_names 为空），已跳过注入工具。"
                    "如需强制注入，可关闭插件配置里的 only_when_kb_configured。"
                )
            return

        tool = self.context.get_llm_tool_manager().get_builtin_tool(KB_TOOL_NAME)

        if tool_set is None:
            req.func_tool = ToolSet()  # type: ignore[attr-defined]
            tool_set = req.func_tool
        tool_set.add_tool(tool)

    def _kb_configured(self) -> bool:
        """检查是否真的配置了知识库。"""
        try:
            cfg = self.context.get_config()
            names = cfg.get("kb_names") or []
            if not names:
                return False
            return True
        except Exception:  # noqa: BLE001
            # 读不到配置就不拦，交给核心处理
            return True

    async def terminate(self) -> None:
        logger.info("[KBTool] 插件已终止")
