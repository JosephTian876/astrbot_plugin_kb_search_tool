"""知识库检索常驻 —— 让 ``astr_kb_search`` 工具始终可用。

做什么：在 ``on_llm_request`` 里把核心内置工具 ``astr_kb_search`` 追加进本次请求的
``func_tool``，于是 ``kb_agentic_mode=false``（只有自动注入、模型没有工具）时它也有了。
不做什么：不改 ``kb_agentic_mode``、不碰核心的自动注入、不改工具实现、不 monkeypatch。
前提：内置 Agent（``runner_type=local``）+ provider 支持函数调用；插件无资源需清理。
背景、适用范围与排查方法见 ``README.md``。
"""

from __future__ import annotations

from astrbot.api import FunctionTool, ToolSet, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

#: 核心内置工具名。``kb_agentic_mode=true`` 时核心注册的是同一个类的同一个实例，
#: 本插件只是把它交给模型，所以不会出现第二份实现。
KB_TOOL_NAME = "astr_kb_search"

#: 核心按 priority 降序执行钩子（``core/star/star_handler.py``：
#: ``sort(key=lambda h: -h.extras_configs["priority"])``），数值越大越先、负数越晚，
#: 所以取足够小的负数让自己排在普通插件（0）之后，但这**不保证绝对最后**；
#: "不被同名工具覆盖"靠的是下面先用 ``get_tool`` 判重（``ToolSet.add_tool`` 对同名是覆盖语义）。
HOOK_PRIORITY = -310000


class KBSearchToolPlugin(Star):
    """把内置的知识库检索工具常驻到每次 LLM 请求。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        raw = dict(config or {})
        self.enabled: bool = bool(raw.get("enable", True))
        self.only_when_kb_configured: bool = bool(
            raw.get("only_when_kb_configured", True)
        )
        #: 缓存的核心工具实例；``None`` = 上次没取到，下个请求会再试（自愈）。
        self._tool: FunctionTool | None = None
        # 两个"只报一次"的标记：取工具失败、无知识库。默认配置下每请求都会命中，逐次打会刷屏。
        self._resolve_failed = False
        self._warned_no_kb = False

        logger.info("[KBTool] 已加载 | 启用=%s | 工具=%s", self.enabled, KB_TOOL_NAME)
        # 启动即解析一次，让"核心没注册该工具"当场暴露，而不是等第一个请求静默不注入。
        # 放在 INFO 之后：故障时日志最后一行是 ERROR，不会被"宣示成功"盖住。
        self._resolve_tool()

    @filter.on_llm_request(priority=HOOK_PRIORITY)
    async def on_llm_request(self, event: AstrMessageEvent, req: object) -> None:
        """确保本次请求的工具集中含有 astr_kb_search。"""
        # req 是核心的 ProviderRequest，但它不在 astrbot.api 公开面里，故只按鸭子类型用；
        # 这里必须 fail-open：注入失败最坏只是模型少一个工具，绝不能打断正常对话。
        try:
            await self._ensure_tool(event, req)
        except Exception as exc:  # noqa: BLE001 - 异常类型不可预期
            logger.error(
                "[KBTool] 注入工具时异常，已跳过本次注入: %s", exc, exc_info=True
            )

    async def _ensure_tool(self, event: AstrMessageEvent, req: object) -> None:
        if not self.enabled:  # 关闭时彻底不介入，痕迹只有启动那行"启用=False"
            return

        tool = self._resolve_tool()
        if tool is None:
            return  # 原因与补救办法已由 _resolve_tool 打过一次

        tool_set = getattr(req, "func_tool", None)
        if tool_set is not None and tool_set.get_tool(KB_TOOL_NAME) is not None:
            # 核心 agentic 模式或别的钩子已加过：不重复添加，也不替换别人的实例。
            return

        if self.only_when_kb_configured and not await self._kb_available(event):
            if not self._warned_no_kb:
                self._warned_no_kb = True
                logger.warning(
                    "[KBTool] 当前会话与主配置都没有可用知识库，已跳过注入工具；"
                    "如需强制注入请关闭 only_when_kb_configured。"
                )
            return

        if tool_set is None:  # 纯对话没有别的工具时核心给的就是 None，不新建等于缺席
            req.func_tool = ToolSet()  # type: ignore[attr-defined]
            tool_set = req.func_tool
        tool_set.add_tool(tool)
        logger.debug("[KBTool] 注入 %s，工具数=%d", KB_TOOL_NAME, len(tool_set.tools))

    def _resolve_tool(self) -> FunctionTool | None:
        """取核心内置工具实例，成功即缓存。

        失败时**不**永久停用自己：``get_builtin_tool`` 会顺带导入全部内置工具模块，任一模块
        导入失败都会让启动瞬间取不到工具；环境修好后应当自愈，而不是整个进程寿命内静默失效。
        """
        if self._tool is not None:
            return self._tool
        try:
            self._tool = self.context.get_llm_tool_manager().get_builtin_tool(
                KB_TOOL_NAME
            )
        except Exception as exc:  # noqa: BLE001 - 核心注册表的异常类型不可预期
            if not self._resolve_failed:
                self._resolve_failed = True
                logger.error(
                    "[KBTool] 取不到内置工具 %s（后续请求会自动重试）: %s；"
                    "请确认 AstrBot >= 4.23 且核心内置工具可正常导入。",
                    KB_TOOL_NAME,
                    exc,
                )
            return None
        return self._tool

    async def _kb_available(self, event: AstrMessageEvent) -> bool:
        """当前会话有没有可用知识库，判定顺序与核心 ``retrieve_knowledge_base`` 一致。

        核心先看会话级 ``kb_config.kb_ids``，没有该键才回落到主配置 ``kb_names``
        （``core/tools/knowledge_base_tools.py``）。不镜像会两头出错：只配会话级知识库的会话
        被拒绝注入，会话里显式关掉知识库（``kb_ids=[]``）却仍注入。异常一律返回 True
        （fail-open）：多注入最多多一次无效调用，判定失败就不注入则会让插件整体失效。
        """
        try:
            umo = event.unified_msg_origin
            session_cfg = await sp.session_get(umo, "kb_config", default={})
            if isinstance(session_cfg, dict) and "kb_ids" in session_cfg:
                return bool(session_cfg.get("kb_ids"))
            return bool(self.context.get_config(umo=umo).get("kb_names") or [])
        except Exception as exc:  # noqa: BLE001 - 见 docstring 的 fail-open
            # 用 debug 而非 warning：持续失败会每请求一条；排障时把插件日志调到 DEBUG 即可。
            logger.debug("[KBTool] 判定知识库可用性失败，按可用处理: %s", exc)
            return True
