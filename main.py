"""知识库检索常驻 —— 让 ``astr_kb_search`` 工具始终可用。

做什么：在 ``on_llm_request`` 里把核心内置工具 ``astr_kb_search`` 追加进本次请求的
``func_tool``，于是 ``kb_agentic_mode=false``（只有自动注入、模型没有工具）时它也有了。
不做什么：不改 ``kb_agentic_mode``、不碰核心的自动注入、不改工具实现、不 monkeypatch。
前提：内置 Agent（``runner_type=local``）；当前 provider 的「模型能力」若**明确**没勾
``tool_use``，默认跳过注入（``respect_provider_tool_support=false`` 可关掉该判定）。
插件无资源需清理。背景、适用范围与排查方法见 ``README.md``。
"""

from __future__ import annotations

from astrbot.api import FunctionTool, ToolSet, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

#: 核心内置工具名。``kb_agentic_mode=true`` 时核心注册的是同一个类的同一个实例，
#: 本插件只是把它交给模型，所以不会出现第二份实现。
KB_TOOL_NAME = "astr_kb_search"

#: 核心按 priority 降序执行钩子（``core/star/star_handler.py``：
#: ``sort(key=lambda h: -h.extras_configs["priority"])``），数值越大越先、负数越晚，
#: 所以取足够小的负数让自己排在普通插件（0）之后，但这**不保证绝对最后**。
#: 判重保护的是"我们不去覆盖别人"；被别人覆盖只能靠排在后面，而这一点并不保证。
HOOK_PRIORITY = -310000

#: provider 明确声明"支持函数调用"时用的模态名。核心清空工具的条件是
#: ``isinstance(modalities, list) and modalities and "tool_use" not in modalities``
#: （4.29.1 ``core/agent/runners/tool_loop_agent_runner.py:680-686``）。
TOOL_USE_MODALITY = "tool_use"

#: 图片模态名：``req.image_urls`` 非空且主 provider 没有 ``image`` 时，核心会把这次请求
#: 切给 fallback provider（``core/astr_main_agent.py:1412-1443``），能力就不再是"当前 provider"。
IMAGE_MODALITY = "image"

#: ``provider_config`` 里没有 ``modalities`` 这个键（与"键存在但值是 None"区分开）。
_MODALITIES_MISSING = object()


class KBSearchToolPlugin(Star):
    """把内置的知识库检索工具常驻到每次 LLM 请求。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        raw = dict(config or {})
        self.enabled: bool = bool(raw.get("enable", True))
        self.only_when_kb_configured: bool = bool(
            raw.get("only_when_kb_configured", True)
        )
        #: 注入前尊重 provider 的「模型能力」声明：只有**明确**没勾 ``tool_use`` 才跳过。
        self.respect_provider_tool_support: bool = bool(
            raw.get("respect_provider_tool_support", True)
        )
        #: 缓存的核心工具实例；``None`` = 上次没取到，下个请求会再试（自愈）。
        self._tool: FunctionTool | None = None
        # 三个"只报一次"的标记：取工具失败、无知识库、provider 判定里的同一种未知原因
        # 实测每请求都会命中，逐次打会刷屏。
        self._resolve_failed = False
        self._warned_no_kb = False
        self._unknown_logged: set[str] = set()
        #: "明确不支持工具"的 WARNING 去重键 ``(provider_id, model)``：用户切了模型要再说一次。
        self._warned_unsupported: set[tuple[str, str]] = set()

        logger.info("[KBTool] 已加载 | 启用=%s | 工具=%s", self.enabled, KB_TOOL_NAME)
        # 启动即解析一次，让"核心没注册该工具"当场暴露，而不是等第一个请求静默不注入。
        # 放在 INFO 之后：故障时日志最后一行是 ERROR，不会被"宣示成功"盖住。
        self._resolve_tool()

    @filter.on_llm_request(priority=HOOK_PRIORITY)
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """确保本次请求的工具集中含有 astr_kb_search。"""
        # req 是公开面类型 ProviderRequest（字段定义见 core/provider/entities.py）；
        # 这里必须 fail-open：注入失败最坏只是模型少一个工具，绝不能打断正常对话。
        try:
            await self._ensure_tool(event, req)
        except Exception as exc:  # noqa: BLE001 - 异常类型不可预期
            logger.error(
                "[KBTool] 注入工具时异常，已跳过本次注入: %s", exc, exc_info=True
            )

    async def _ensure_tool(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.enabled:  # 关闭时彻底不介入，痕迹只有启动那行"启用=False"
            return

        tool = self._resolve_tool()
        if tool is None:
            return  # 原因与补救办法已由 _resolve_tool 打过一次

        tool_set = req.func_tool
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

        # 放在最后一步之前：只有"确实要注入"的请求才付这次判定成本（知识库判定更常见）。
        if self.respect_provider_tool_support and not await self._provider_tool_support(
            event, req
        ):
            return

        if tool_set is None:  # 纯对话没有别的工具时核心给的就是 None，不新建等于缺席
            req.func_tool = ToolSet()
            tool_set = req.func_tool
        tool_set.add_tool(tool)
        # 日志取值也不假设一定有 ``tools``：func_tool 可能被别的插件换成自定义对象。
        tool_count = len(getattr(tool_set, "tools", ()))
        logger.debug("[KBTool] 注入 %s，工具数=%d", KB_TOOL_NAME, tool_count)

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

    async def _provider_tool_support(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> bool:
        """当前 provider 是否支持函数调用：``True``=继续注入，``False``=确定不支持。

        **只有** ``modalities`` 是"非空 list 且不含 ``tool_use``"时才返回 ``False``。其余
        一律 ``True``（fail-open，按支持处理），因为它们是"读不到/没配置"而不是"不支持"：

        - ``modalities`` 为 ``None``/``[]``/缺键：核心 4.29 明确当"未配置，默认支持全部
          模态"（``tool_loop_agent_runner.py:665-669``），按否定信号处理会让绝大多数没填
          「模型能力」的用户整个插件失效；
        - 非 list（字符串/字典/tuple…）：非法配置，核心只认 ``isinstance(list)``；
        - 取不到 provider、``provider_config`` 不是 dict、查询抛异常：未知。

        本方法**绝不抛异常**：外层虽有兜底，但探测失败是预期路径，靠外层会每请求一条 ERROR。
        """
        try:
            found, provider = await self._actual_provider(event)
            if not found:
                # 调用方（旧核心/自定义 stub/契约冒烟）根本没有 provider 查询能力：
                # 静默放行。多打一条 DEBUG 会破坏 test_01 的"成功注入恰好 1 条 DEBUG"。
                return True
            if provider is None:
                return self._assume_supported(
                    "provider=None", "取不到 provider（provider=None）"
                )
            config = getattr(provider, "provider_config", None)
            if not isinstance(config, dict):
                return self._assume_supported(
                    f"config:{type(config).__name__}",
                    f"provider_config 不是 dict（类型={type(config).__name__}）",
                )
            modalities = config.get("modalities", _MODALITIES_MISSING)
            if modalities is None or modalities is _MODALITIES_MISSING:
                return self._assume_supported(
                    "modalities:unset", "modalities 缺键或为 None（未配置）"
                )
            if not isinstance(modalities, list):
                return self._assume_supported(
                    f"modalities:type:{type(modalities).__name__}",
                    f"modalities 类型={type(modalities).__name__}（非法配置，核心只认 list）",
                )
            if not modalities:
                return self._assume_supported(
                    "modalities:empty", "modalities=[]（未配置）"
                )
            if TOOL_USE_MODALITY in modalities:
                return True  # 明确支持：正常路径，不额外打日志
            if getattr(req, "image_urls", None) and IMAGE_MODALITY not in modalities:
                # 这张图会被核心切给 fallback provider，那个 provider 的能力未知 → 放行。
                return self._assume_supported(
                    "image:fallback", "图片请求可能切到 fallback provider，能力未知"
                )
            self._warn_unsupported_once(provider, config)
            return False
        except Exception as exc:  # noqa: BLE001 - 探测失败必须 fail-open（AUDIT-KB §3.6）
            return self._assume_supported(
                f"exc:{type(exc).__name__}", f"{type(exc).__name__}: {exc}"
            )

    async def _actual_provider(self, event: AstrMessageEvent) -> tuple[bool, object]:
        """本次请求实际会用的 provider：``(是否有查询能力, provider)``。

        顺序与核心 ``_select_provider`` 一致（``core/astr_main_agent.py:283-310``）：会话级
        ``selected_provider``（WebUI/开放 API 选模型）优先，否则按 umo 取当前 provider。
        不镜像会在用户切模型时判错对象。

        版本兼容（本插件声明 ``>=4.23``，见 ``metadata.yaml``）：

        - ``get_using_provider_async`` **4.27.5 才有**；4.23–4.26 的 ``Context`` 只有同步
          ``get_using_provider``（4.23.0 源码：``core/star/context.py:359``）。所以必须
          ``getattr`` 探测 + 回退 —— 无条件 ``await`` 会在声明下界上每请求 ``AttributeError``。
        - 同步版内部走 ``sp.get`` 的**专用同步 SQLite 连接**做一次点查（``shared_preferences``
          注明"briefly blocks the calling thread"，但不碰 async 连接池、不会死锁事件循环），
          实测约 1 ms；只有 4.27.5 以下才会走到。
        - 两个方法都没有 → ``(False, None)``：调用方静默放行（见 ``_provider_tool_support``）。
        """
        umo = getattr(event, "unified_msg_origin", None)
        get_extra = getattr(event, "get_extra", None)
        selected = get_extra("selected_provider") if callable(get_extra) else None
        if isinstance(selected, str) and selected:
            get_by_id = getattr(self.context, "get_provider_by_id", None)
            selected_provider = get_by_id(selected) if callable(get_by_id) else None
            if selected_provider is not None:
                return True, selected_provider
            # 选中的 provider 已被删除：回落到会话默认 provider（核心此时会直接报错，
            # 但"未知就放行"对本插件更安全）。
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            return True, await getter(umo=umo)
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            return True, getter(umo=umo)
        return False, None

    def _assume_supported(self, key: str, reason: str) -> bool:
        """fail-open：按"支持工具"处理，同一个原因只 DEBUG 一次（返回值恒为 True）。"""
        if key not in self._unknown_logged:
            self._unknown_logged.add(key)
            logger.debug(
                "[KBTool] 无法判定当前 provider 是否支持工具调用（%s），按支持处理。",
                reason,
            )
        return True

    def _warn_unsupported_once(self, provider: object, config: dict) -> None:
        """「确定不支持」的 WARNING：每个 provider+model 一次。

        这是"插件为什么没生效"的唯一可见原因，必须可行动 —— 文案里带 provider id/model、
        判据 ``tool_use``，以及自救开关名。但不能每请求刷屏。
        """
        model = config.get("model")
        provider_id = config.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            provider_id = f"{type(provider).__name__}@{model}"
        key = (provider_id, str(model))
        if key in self._warned_unsupported:
            return
        self._warned_unsupported.add(key)
        logger.warning(
            "[KBTool] 当前 provider「%s」（模型 %s）的模型能力未勾选 tool_use，已跳过注入；"
            "如需强制注入请关闭 respect_provider_tool_support。",
            provider_id,
            model,
        )
