"""知识库检索常驻 —— 让 ``astr_kb_search`` 工具始终可用。

做什么：在 ``on_llm_request`` 里把核心内置工具 ``astr_kb_search`` 追加进本次请求的
``func_tool``，于是 ``kb_agentic_mode=false``（只有自动注入、模型没有工具）时它也有了。
不做什么：不改 ``kb_agentic_mode``、不碰核心的自动注入、不改工具实现、不 monkeypatch。
前提：内置 Agent（``runner_type=local``）；当前 provider 的「模型能力」若**明确**没勾
``tool_use``，默认跳过注入（``respect_provider_tool_support=false`` 可关掉该判定）。
插件无资源需清理。背景、适用范围与排查方法见 ``README.md``。
"""

from __future__ import annotations

from collections.abc import Mapping

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

#: 插件认识的配置键。多出来的键（拼写错误）会一次性 WARNING，而不是静默忽略 ——
#: 用户以为改的是闸门、实际写了个不存在的名字，是这一类配置最难自查的故障。
_KNOWN_CONFIG_KEYS = frozenset(
    {"enable", "only_when_kb_configured", "respect_provider_tool_support"}
)

#: 布尔配置值的显式白名单。``bool("false") is True`` 会静默反转用户意图（手改 JSON 时
#: 最常见），所以**不用** ``bool()`` 做解析：只认这些写法，其余一律按"无法识别"处理。
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


#: ``_unknown_logged`` 的上界。键里含 ``str(exc)[:80]``，只要上游异常消息带可变内容
#: （端口、超时毫秒、umo、URL），每条都是一次新键 → set 会被**外部输入**无限喂大。
#: 到顶后清空重来（而不是"不再记新键"）：旧原因被重新记一次只是多几条 DEBUG，而永久丢弃
#: 新原因会让排障时看不到"当前真正在报什么"。
_UNKNOWN_LOG_CAP = 64


def parse_bool_setting(value: object) -> bool | None:
    """把配置里的布尔值解析成 ``bool``；认不出返回 ``None``（由调用方回落到默认值）。

    认的写法：真 ``bool``、整数 ``1``/``0``、以及（忽略大小写与首尾空白的）
    ``"1"``/``"0"``/``"true"``/``"false"``/``"yes"``/``"no"``/``"on"``/``"off"``。

    为什么不去猜：``1.5``/``2``/``[]``/``{}``/``"maybe"`` 这些值的"真值"纯属 Python 语义
    巧合，用户真正想要的多半是另一个值 —— 回到默认值并告警一次，比猜错后反向生效安全。
    """
    if isinstance(value, bool):  # 必须先于 int：bool 是 int 的子类
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None


def all_providers_declare_tool_support(providers: object) -> bool:
    """静态预筛（O1）：可枚举的 chat provider 里，**没有任何一个**声明"不支持工具"。

    "声明不支持"用的判据与 ``_provider_tool_support`` 逐字相同：``modalities`` 是**非空
    list 且不含 ``tool_use``**。只要一个这样的 provider 存在，本函数就返回 ``False``。

    返回 ``False`` 的含义只有一个：**无法证明**"本次请求必然判为支持"，调用方必须回到
    原来的路径（查一次 provider）。它绝不表示"不支持"：这里只是"别省这一步"的信号。
    因此它的错误方向只可能是"多查一次 DB"（安全），不可能是"误放行"（危险）。

    安全论证（为什么"扫全量 chat provider 都没声明不支持"就能跳过查询）：

    - 核心解析 provider 只有三条出口：会话级 ``selected_provider`` → ``inst_map.get(id)``、
      会话偏好 → ``inst_map.get(id)``、都没有 → ``provider_insts[0]``
      （4.29.1 ``core/provider/manager.py:245-258``）。
    - 每个成功加载的 provider 同时进 ``inst_map``（``:812``）与（chat 类型）
      ``provider_insts``（``:778``），禁用的根本不加载（``:670-672``）。而公开面
      ``Context.get_all_providers()`` 返回的就是 ``provider_insts``（``core/star/context.py``；
      4.23.0 同款），所以它是 chat provider 的**超集**。
    - 例外是"会话级 ``selected_provider`` 指向非 chat provider"（``get_provider_by_id``
      能取到 TTS/Embedding 实例）：那种对象不在 ``get_all_providers()`` 里，无法扫到。
      调用方因此只在**没有** ``selected_provider`` 时才用本预筛（见
      ``_static_prefilter_proves_support``），把这条边角彻底排除。

    保守性细节（都不影响正确性，只是别把预筛做激进）：

    - ``providers`` 为 ``None`` / 不可迭代 / 迭代中途抛异常 → ``False``（退回查 DB）；
    - ``providers`` 是 Mapping（例如有人把 ``inst_map`` 传进来）→ ``False``：对 dict 迭代
      拿到的是**键**，``getattr(key, "provider_config")`` 自然是 None，于是静默得出"全体
      未声明"——那就成了误放行。宁可多查一次。
    - ``provider_config`` 不是 dict、``modalities`` 缺键/``None``/``[]``/非 list → 跳过该
      provider：在原判定路径里这些也都是"未知 → 按支持处理"，不是否定信号，跳过后语义一致。
    """
    if providers is None or isinstance(providers, Mapping):
        return False
    try:
        iterator = iter(providers)  # type: ignore[arg-type] - 运行时探测，非静态可判
    except TypeError:
        return False
    try:
        for provider in iterator:
            config = getattr(provider, "provider_config", None)
            if not isinstance(config, dict):
                continue  # 读不到配置 = 未知；原路径同样按"支持"处理
            modalities = config.get("modalities", _MODALITIES_MISSING)
            if not isinstance(modalities, list) or not modalities:
                continue  # 缺键 / None / 空 list / 非 list：都不是否定信号
            if TOOL_USE_MODALITY not in modalities:
                return False  # 有一个"明确不支持" → 无法证明全体支持，退回查 DB
    except Exception:  # noqa: BLE001 - 预筛失败只能退回原路径，绝不能碰瓷主流程
        return False
    return True


def selected_provider_id(event: object) -> str | None:
    """会话级选中的 provider id（WebUI/开放 API 选模型时才有），没有则 ``None``。

    提取成独立函数是为了让"预筛能不能用"与"实际判定用哪个 provider"读**同一份**逻辑：
    两处若各写一份 ``get_extra`` 判断，迟早会在某个边界（空串、非 str）上分叉。
    """
    get_extra = getattr(event, "get_extra", None)
    selected = get_extra("selected_provider") if callable(get_extra) else None
    return selected if isinstance(selected, str) and selected else None


def describe_provider(provider: object) -> tuple[str, str, str]:
    """``(provider_id, model, 显示用标签)``：从 provider 的配置里取，取不到给可读兜底。

    只给日志用，**不参与任何判定**。``provider_id`` 缺失时用 ``"<类名>@<model>"`` 兜底 —
    与"明确不支持"那条 WARNING 的兜底同形，用户在两行日志里看到的是同一个名字。
    """
    config = getattr(provider, "provider_config", None)
    config = config if isinstance(config, dict) else {}
    model = config.get("model")
    provider_id = config.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        provider_id = f"{type(provider).__name__}@{model}"
    model_text = "<未配置>" if model in (None, "") else str(model)
    return provider_id, model_text, f"{provider_id}（模型 {model_text}）"


class KBSearchToolPlugin(Star):
    """把内置的知识库检索工具常驻到每次 LLM 请求。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        raw = dict(config or {})
        self.enabled: bool = self._read_bool(raw, "enable", True)
        self.only_when_kb_configured: bool = self._read_bool(
            raw, "only_when_kb_configured", True
        )
        #: 注入前尊重 provider 的「模型能力」声明：只有**明确**没勾 ``tool_use`` 才跳过。
        self.respect_provider_tool_support: bool = self._read_bool(
            raw, "respect_provider_tool_support", True
        )
        self._warn_unknown_config_keys(raw)
        #: 缓存的核心工具实例；``None`` = 上次没取到，下个请求会再试（自愈）。
        self._tool: FunctionTool | None = None
        # 四个"只报一次"的标记：取工具抛异常、取工具返回 None、无知识库、provider 判定里的
        # 同一种未知原因 —— 实测每请求都会命中，逐次打会刷屏。
        self._resolve_failed = False
        self._resolve_missing = False
        self._warned_no_kb = False
        self._unknown_logged: set[str] = set()
        #: "明确不支持工具"的 WARNING 去重键 ``(provider_id, model)``：用户切了模型要再说一次。
        self._warned_unsupported: set[tuple[str, str]] = set()
        #: 本次请求判定过的 provider（``(provider_id, model, 显示用标签)``）。
        #: 只用于"注入成功"那条 DEBUG 里的"判定对象=..."，让用户排障时一眼看到判的是谁。
        #:
        #: 为什么这里用实例状态是安全的：写入发生在 ``_provider_tool_support`` 里最后一次
        #: ``await`` **之后**，读取发生在 ``_ensure_tool`` 的下一行 —— 中间没有任何 await 点，
        #: 而 asyncio 只在 await 处切换任务，所以不会串到别的会话上。进入 ``_ensure_tool``
        #: 时先置 ``None``，保证开关关闭/预筛命中时不会残留上一次请求的标签。
        self._judged_provider: tuple[str, str, str] | None = None

        logger.info("[KBTool] 已加载 | 启用=%s | 工具=%s", self.enabled, KB_TOOL_NAME)
        # 启动即解析一次，让"核心没注册该工具"当场暴露，而不是等第一个请求静默不注入。
        # 放在 INFO 之后：故障时日志最后一行是 ERROR，不会被"宣示成功"盖住。
        self._resolve_tool()

    @staticmethod
    def _read_bool(raw: dict, key: str, default: bool) -> bool:
        """读一个布尔配置项：走显式白名单，认不出就用默认值并**告警一次**。

        只在 ``__init__`` 里调用（启动/重载各一次），因此天然不会按请求刷屏。
        真 ``bool`` 走的是第一个分支、原样返回 —— 与 0.2.0 的行为逐字节相同，
        这是"开关关闭 == 0.2.0"等价性的前提。
        """
        value = raw.get(key, default)
        parsed = parse_bool_setting(value)
        if parsed is not None:
            return parsed
        logger.warning(
            "[KBTool] 配置项 %s 的值无法识别（%s=%r），已按默认 %s 处理；"
            '请填 JSON 布尔值 true/false（也认 1/0 与 "true"/"false"/"yes"/"no"/'
            '"on"/"off"）。',
            key,
            type(value).__name__,
            value,
            default,
        )
        return default

    @staticmethod
    def _warn_unknown_config_keys(raw: dict) -> None:
        """未知配置键（多半是拼写错误）一次性列出，避免"改了却没生效"。

        **真实环境的实际情况（实测，不是推测）**：正常加载路径上核心的 ``AstrBotConfig``
        已经先一步把 schema 之外的键**删掉并写回文件**，同时打一条 INFO
        （4.29.1 ``Config key removed: <key>``；4.23.0 ``检查到配置项 <key> 不存在，将从
        当前配置中删除``）—— 所以这条 WARNING 在正常安装里通常**不会**出现。它守的是另外
        两种入口：核心直接把 dict 交给插件（测试、第三方加载器、契约冒烟），以及将来核心
        改变清理行为。多一条自述性告警的成本可以忽略，而"改了没生效且没人说"很难自查。
        证据：``tests/probe_core_config.py``（在两个真实版本树上各跑一次）。

        只提示、不改行为：真正的开关仍由上面三个已知键决定。``str(key)`` 兜底是因为
        配置理论上可能来自非 JSON 来源（键不是 ``str`` 时 ``sorted`` 会直接抛）。
        """
        unknown = sorted(str(key) for key in raw if key not in _KNOWN_CONFIG_KEYS)
        if not unknown:
            return
        logger.warning(
            "[KBTool] 配置里有无法识别的键：%s（已忽略）；可用键只有：%s。"
            "键名拼错时那一项不会生效，请对照插件配置页核对。",
            "、".join(unknown),
            "、".join(sorted(_KNOWN_CONFIG_KEYS)),
        )

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
        self._judged_provider = None  # 清零：见 __init__ 里关于本次请求标签的说明
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
                    "（本条每个插件生命周期只提示一次：之后别的会话遇到同样情况不再重复。）"
                )
            return

        # 放在最后一步之前：只有"确实要注入"的请求才付这次判定成本（知识库判定更常见）。
        if self.respect_provider_tool_support:
            # O1 静态预筛：已加载的 chat provider 里没人声明"缺 tool_use"时，无论核心这次
            # 解析到哪一个，判定结果都只能是"支持" —— 那次 DB 点查（实测约 1.12 ms）可以
            # 整段跳过。预筛证明不了就照旧查一次，绝不改变"判定为不支持"的语义。
            if self._static_prefilter_proves_support(event):
                judged = "未查询（静态预筛：已加载 provider 均未声明缺 tool_use）"
            else:
                if not await self._provider_tool_support(event, req):
                    return
                judged = (
                    self._judged_provider[2]
                    if self._judged_provider is not None
                    else "未知（原因见上一条 DEBUG）"
                )
        else:
            judged = "未查询（respect_provider_tool_support=false）"

        if tool_set is None:  # 纯对话没有别的工具时核心给的就是 None，不新建等于缺席
            req.func_tool = ToolSet()
            tool_set = req.func_tool
        tool_set.add_tool(tool)
        # 日志取值也不假设一定有 ``tools``：func_tool 可能被别的插件换成自定义对象。
        tool_count = len(getattr(tool_set, "tools", ()))
        # 带上"判定对象"：用户按 README 调 DEBUG 排障时，要能一眼看出这次判的是哪个
        # provider/model，而不是只知道"注入了"。没查时不编造，写明原因。
        logger.debug(
            "[KBTool] 注入 %s，工具数=%d，判定对象=%s", KB_TOOL_NAME, tool_count, judged
        )

    def _resolve_tool(self) -> FunctionTool | None:
        """取核心内置工具实例，成功即缓存。

        失败时**不**永久停用自己：``get_builtin_tool`` 会顺带导入全部内置工具模块，任一模块
        导入失败都会让启动瞬间取不到工具；环境修好后应当自愈，而不是整个进程寿命内静默失效。

        两条失败路径分开报（修法不同）：**抛异常**用 ERROR（注册表/导入坏了），
        **返回 None** 用 WARNING（名字没登记，实测核心只会抛异常，所以这属理论分支，
        但它一旦发生就是"每请求重试 + 零日志"的静默失效）。
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
        if self._tool is None:
            # 与异常分支区别：这里注册表本身是好用的，只是没能给出这个名字。
            if not self._resolve_missing:
                self._resolve_missing = True
                logger.warning(
                    "[KBTool] 内置工具注册表返回 None（没有抛异常），即当前核心没有登记 %s；"
                    "本次及后续请求都会自动重试，暂时不注入。"
                    "请确认 AstrBot >= 4.23 且该内置工具未被移除。",
                    KB_TOOL_NAME,
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

    def _static_prefilter_proves_support(self, event: AstrMessageEvent) -> bool:
        """O1 静态预筛：能否**不查 provider** 就断言"本次必然判为支持"。

        ``True`` = 已加载的 chat provider 里没有任何一个声明"缺 ``tool_use``"，于是无论核心
        这次解析到哪一个，``_provider_tool_support`` 都只会返回 ``True`` —— 那次 DB 点查
        （实测 median 约 1.36 ms/请求）可以整段跳过。

        ``False`` = **证明不了**，调用方照旧查一次。这条路径没有任何"判为不支持"的能力：
        最坏结果只是多查一次 DB，所以它不可能放宽 ``respect_provider_tool_support``。

        为什么有 ``selected_provider`` 时直接放弃预筛：会话级选中走
        ``context.get_provider_by_id()``，它能返回 ``inst_map`` 里的**非 chat** provider
        （TTS/Embedding），那类实例不在 ``get_all_providers()`` 的返回里，扫不到也就证明
        不了。这类请求（WebUI/开放 API 显式选模型）本来就少，退回查一次 DB 是安全的。

        为什么要 ``getattr`` 探测：``get_all_providers`` 不是本插件声明下界（4.23.0）之外
        必须存在的私有面 —— 自定义 Context 替身/第三方加载器可能没有它，缺了就跳过预筛。
        """
        if selected_provider_id(event) is not None:
            return False
        getter = getattr(self.context, "get_all_providers", None)
        if not callable(getter):
            return False
        try:
            return all_providers_declare_tool_support(getter())
        except Exception:  # noqa: BLE001 - 预筛失败只能退回查 DB
            return False

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
            # 记下"判的是谁"，供注入成功那条 DEBUG 使用（写在这里 = 最后一次 await 之后，
            # 到读取之间没有 await 点，不会串到别的会话）。provider 为 None 时不记：
            # 那种情况下"判定对象"就是"没有"，让调用方写"未知（原因见上一条 DEBUG）"更诚实。
            if provider is not None:
                self._judged_provider = describe_provider(provider)
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
        except Exception as exc:  # noqa: BLE001 - 探测失败必须 fail-open
            # fail-open 的理由：读不到 provider 只说明"不知道"，不说明"不支持"。按否定信号
            # 处理会让一次数据库抖动变成整段对话失去工具，而多注入最多多一次无效调用。
            # 去重键 = 类型 + 消息前 80 字：只按类型去重时，"同类型不同原因"只留第一条，
            # 排障时看不到真正的原因。截断保证键不会无限长。
            return self._assume_supported(
                f"exc:{type(exc).__name__}:{str(exc)[:80]}",
                f"{type(exc).__name__}: {exc}",
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
          实测 median **1.6158 ms**（p95 2.2301 ms），期间同 loop 的其他协程得不到调度；
          **只有 4.23–4.26** 会走到（4.27.5+ 有 async 版，实测 median 1.0791 ms、不阻塞）。
        - 两个方法都没有 → ``(False, None)``：调用方静默放行（见 ``_provider_tool_support``）。
        """
        umo = getattr(event, "unified_msg_origin", None)
        selected = selected_provider_id(event)
        if selected is not None:
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
        """fail-open：按"支持工具"处理，同一个原因只 DEBUG 一次（返回值恒为 True）。

        ``_unknown_logged`` 有上界（``_UNKNOWN_LOG_CAP``）：键里含异常消息摘要，上游只要给出
        带可变内容的异常（端口、超时毫秒、umo、URL），每次请求都能造出一个新键。到顶就清空
        重来 —— 这只影响"同一条 DEBUG 会不会再出现一次"，不会影响任何判定结果。
        """
        if key not in self._unknown_logged:
            if len(self._unknown_logged) >= _UNKNOWN_LOG_CAP:
                self._unknown_logged.clear()
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
