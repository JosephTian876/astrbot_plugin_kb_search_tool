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

#: 核心 ``ProviderType`` 里**不会**进 ``provider_insts``（chat 列表）的那几个取值
#: （4.23.0 与 4.29.1 的 ``core/provider/entities.py`` 逐字相同），外加 ``load_provider``
#: 显式跳过的伪类型 ``agent_runner``。预筛用它把配置项分成"会被加载成 chat provider"与
#: "不会"两类 —— 后者进的是 ``tts_provider_insts``/``embedding_provider_insts`` 等别的列表，
#: 不能参与"chat 列表是否完整"的比对。
#:
#: 为什么 chat provider 的配置里通常**没有** ``provider_type`` 这个键：注册装饰器
#: ``register_provider_adapter`` 的 ``default_config_tmpl`` 只写 ``type``/``id``，
#: ``provider_type`` 默认就是 ``CHAT_COMPLETION``（4.29.1 ``core/provider/register.py:14-49``），
#: 所以 WebUI 生成的 chat 配置项**不带**该键、而非 chat 配置项带。实测本机一份真实配置
#: （26 个配置项）就是这个形状：7 个非 chat 都有 ``provider_type``，19 个 chat 都没有。
#:
#: 因此判定规则是"**只排除已知的非 chat 取值**"，未列出的取值（含缺键）一律按"可能是
#: chat"处理。判错的方向只能是"少算了一个非 chat 条目 → id 集合对不上 → 多回落一次查 DB"，
#: 不会反过来把 chat provider 漏掉。
_NON_CHAT_PROVIDER_TYPES = frozenset(
    {"speech_to_text", "text_to_speech", "embedding", "rerank", "agent_runner"}
)

#: 插件认识的配置键。多出来的键（拼写错误）会一次性 WARNING，而不是静默忽略 ——
#: 用户以为改的是闸门、实际写了个不存在的名字，是这一类配置最难自查的故障。
_KNOWN_CONFIG_KEYS = frozenset(
    {"enable", "only_when_kb_configured", "respect_provider_tool_support"}
)

#: 布尔配置值的显式白名单。``bool("false") is True`` 会静默反转用户意图（手改 JSON 时
#: 最常见），所以**不用** ``bool()`` 做解析：只认这些写法，其余一律按"无法识别"处理。
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


#: "只报一次"去重集合的上界。键里含 ``str(exc)[:80]``，只要上游异常消息带可变内容
#: （端口、超时毫秒、umo、URL），每条都是一次新键 → 集合会被**外部输入**无限喂大。
#:
#: 到顶后**不再放行新键**（不记日志、也不入集合），代价是排障时看不到"当前真正在报什么"；
#: 换来的是**日志条数**也有上界（总数 ≤ 上界 + 1 条"已达上界"的提示）。此前是"到顶后清空
#: 重来"，集合确实有上界，但 200 个互不相同的原因仍然产出 200 条日志 —— 集合上界不等于
#: 日志上界。一次性提示把"看不见新原因"这件事说出来，比静默刷屏好。
_UNKNOWN_LOG_CAP = 64


class _OnceLog:
    """有界的"只报一次"判据：同一个键放行一次，集合到顶后**不再放行**新键。

    为什么不用裸 ``set`` + ``if key not in s``：那样只约束了内存，不约束日志条数。
    为什么不在到顶后清空重来：清空会让日志条数重新变成"按请求数线性增长"（每个请求都能造
    一个新键），而 R7 验收口径要的是"日志条数 ≤ 上界 + 1"。

    ``saturated`` 让调用方能在**第一次**被拒时补一条一次性提示；``notified`` 保证那条提示
    一个实例只出现一次。
    """

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._keys: set[str] = set()
        #: 是否已经提示过"到上界了"。
        self.notified = False

    def claim(self, key: str) -> bool:
        """该键是否**第一次**出现（``True`` = 调用方应该记这条日志）。"""
        if key in self._keys or len(self._keys) >= self._cap:
            return False
        self._keys.add(key)
        return True

    @property
    def saturated(self) -> bool:
        """集合是否已达上界（此后新键都不会再被放行）。"""
        return len(self._keys) >= self._cap

    def __len__(self) -> int:
        return len(self._keys)


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


def _declares_missing_tool_use(config: object) -> bool:
    """这份 provider 配置是否构成"**明确**不支持工具"（本插件唯一的否定信号）。

    判据与 ``_provider_tool_support`` 逐字相同：``modalities`` 是**非空 list 且不含
    ``tool_use``**。缺键 / ``None`` / ``[]`` / 非 list / 配置不是 Mapping —— 都不是否定信号
    （它们是"未知 → 按支持处理"，与核心 4.29 的"未配置 = 默认支持全部模态"一致）。
    """
    if not isinstance(config, Mapping):
        return False
    modalities = config.get("modalities", _MODALITIES_MISSING)
    return (
        isinstance(modalities, list)
        and bool(modalities)
        and TOOL_USE_MODALITY not in modalities
    )


def all_providers_declare_tool_support(providers: object) -> bool:
    """逐个扫 provider **实例**：可枚举、非空、且没有一个声明"不支持工具"。

    返回 ``False`` 的含义只有一个：**无法证明**"本次请求必然判为支持"，调用方必须回到原来
    的路径（查一次 provider）。它绝不表示"不支持"：这里只是"别省这一步"的信号。

    单独用它**不足以**跳过查询 —— 它只看得到"当前在列表里的实例"，看不到"刚被移出列表、
    但核心仍能解析到"的实例（见 ``prefilter_proves_support`` 里对 terminate 窗口的说明）。
    真正决定"能否跳过"的是 ``prefilter_proves_support``，本函数只是它的一半。

    保守性细节（判错的方向只能是"多查一次 DB"）：

    - 空集合 / ``None`` / 不可迭代 / 迭代中途抛异常 → ``False``。**空集合必须是 False**：
      "没有任何 provider 声明不支持"在空集合上恒真，而空集合恰恰是 provider 重载窗口里
      最常见的形态（单 provider 配置下 ``provider_insts`` 会短暂变成 ``[]``）—— 那是真空真，
      不是证明。
    - ``providers`` 是 Mapping（例如有人把 ``inst_map`` 传进来）→ ``False``：对 dict 迭代
      拿到的是**键**，``getattr(key, "provider_config")`` 自然是 None，于是静默得出"全体
      未声明"——那就成了误放行。宁可多查一次。
    - ``providers`` 是 ``str``/``bytes`` → ``False``：它们可迭代，但迭代出来的字符/字节没有
      ``provider_config``，同样会静默得出"全体未声明"。现实中 ``get_all_providers()`` 不会
      返回它们，这条只是把同一类"形状不对却静默放行"的口子一起堵上。
    """
    if providers is None or isinstance(providers, (Mapping, str, bytes)):
        return False
    try:
        iterator = iter(providers)  # type: ignore[arg-type] - 运行时探测，非静态可判
    except TypeError:
        return False
    seen = False
    try:
        for provider in iterator:
            seen = True
            if _declares_missing_tool_use(getattr(provider, "provider_config", None)):
                return False  # 有一个"明确不支持" → 无法证明全体支持，退回查 DB
    except Exception:  # noqa: BLE001 - 预筛失败只能退回原路径，绝不能碰瓷主流程
        return False
    return seen


def provider_ids(providers: object) -> set[str] | None:
    """``{provider.provider_config["id"]}``；任何一个读不到 ``id`` 就返回 ``None``。

    ``None`` 的含义同样是"证明不了"：id 是"加载表 vs 配置"比对的唯一凭据，缺一个就没法
    确认两边说的是同一批 provider。
    """
    try:
        ids: set[str] = set()
        for provider in providers:  # type: ignore[union-attr] - 运行时探测
            config = getattr(provider, "provider_config", None)
            provider_id = config.get("id") if isinstance(config, Mapping) else None
            if not isinstance(provider_id, str) or not provider_id:
                return None
            ids.add(provider_id)
        return ids
    except Exception:  # noqa: BLE001 - 与纯函数其余部分同姿态：失败即证明不了
        return None


def config_provider_configs(config: object) -> list[dict] | None:
    """配置里的**全部** provider 条目（含禁用的）；任何一处读不懂就返回 ``None``。

    与 ``config_chat_provider_configs`` 的分工：这里只做"能不能安全地读"，不做分类。
    为什么要连**禁用的**条目一起返回：核心禁用某个 provider 时是"先把配置写成
    ``enable=false``、再 ``reload()`` → ``terminate_provider()``"（4.29.1
    ``core/provider/manager.py:950``/``:825``），所以在 ``terminate()`` 那个 await 里，
    配置已经说"它没启用"、而 ``inst_map`` 里那个**旧实例**仍能被解析到。判断"不许放行"时
    必须看得到它 —— 见 ``prefilter_proves_support``。
    """
    providers = config.get("provider") if isinstance(config, Mapping) else None
    if not isinstance(providers, list) or not providers:
        return None
    result: list[dict] = []
    for entry in providers:
        if not isinstance(entry, Mapping):
            return None
        provider_id = entry.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            return None
        provider_type = entry.get("provider_type")
        if provider_type is not None and not isinstance(provider_type, str):
            return None
        if "enable" not in entry:
            return None  # 核心自己会在 load_provider 里 KeyError，这里无从判断
        result.append(dict(entry))
    return result


def config_chat_provider_configs(config: object) -> list[dict] | None:
    """配置里"核心会**加载成 chat provider**"的那些条目；判断不了返回 ``None``。

    过滤规则逐条对齐 ``ProviderManager.load_provider``（4.29.1 ``:663-680`` / 4.23.0
    ``:535-546``）：

    - ``provider_type`` 属于 ``_NON_CHAT_PROVIDER_TYPES`` → 跳过（进的是别的 ``*_insts`` 列表，
      或 ``agent_runner`` 被核心显式 return 掉）；
    - ``enable`` 为假 → 跳过（核心 ``if not provider_config["enable"]: return``，不加载 =
      也就不进 ``inst_map``，稳态下解析不到）；
    - 其余（含缺 ``provider_type`` 的 chat 条目）→ 计入。

    这份列表只用于"加载表是否完整"的**数量/编号比对**。"能不能放行"还要另看
    ``config_provider_configs``（含禁用条目）的声明扫描 —— 禁用条目的实例在 terminate
    窗口里仍可能被解析到，不能因为"没启用"就无视它的能力声明。
    """
    entries = config_provider_configs(config)
    if entries is None:
        return None
    return [
        entry
        for entry in entries
        if entry["enable"]
        and entry.get("provider_type") not in _NON_CHAT_PROVIDER_TYPES
    ]


def prefilter_proves_support(loaded: object, config: object) -> bool:
    """静态预筛（O1）的完整判据：能否**不查 provider** 就断言"本次必然判为支持"。

    四个条件**必须同时**成立，任何一个不成立都只是"证明不了"（照旧查一次 DB），
    绝不表示"不支持"：

    1. 已加载的 chat provider 实例**非空**，且没有一个声明缺 ``tool_use``
       （``all_providers_declare_tool_support``）；
    2. 配置里的 provider 条目**非空**，且**没有任何一条**（含禁用的、含 TTS/Embedding 这类
       非 chat 的）声明缺 ``tool_use``；
    3. 配置里"会被加载成 chat provider"的条目**非空**；
    4. 第 3 条的 ``id`` 集合与已加载实例的 ``id`` 集合**完全相等**（数量与 id 都一致）。

    为什么需要第 2、3、4 条 —— 它们堵的是 ``terminate_provider`` 的窗口：

    核心删/改/重载 provider 时先把实例移出 ``provider_insts``（4.29.1 ``manager.py:883``），
    再 ``await inst.terminate()``（``:901``，openai 源是 ``await client.close()``，**真的会让出
    事件循环**），最后才 ``del inst_map[provider_id]``（``:908``）。窗口期内
    ``Context.get_all_providers()``（= ``provider_insts``）已经看不到它，而
    ``_resolve_using_provider`` 用的 ``inst_map.get(provider_id)`` 仍能解析到它
    （``:245``/``:256``）。只满足第 1 条时就会"证明支持 → 跳过那次点查 → 把工具注入给一个
    声明不支持工具的 provider"，这正是本函数要多看一份**配置快照**的原因：配置的变更
    时机与 ``provider_insts`` 不同（``delete_provider`` 先 terminate 再改配置，``:925``；
    ``update_provider`` 先改配置再 reload，``:950``），所以"加载表 id 集合 == 配置 chat id
    集合"在窗口期内必然不成立。

    第 2 条为什么**不按 enable 过滤**：用户"禁用"一个 provider 时，配置先变成
    ``enable=false``、随后才 ``terminate`` —— 在这个 await 里配置已经说"没启用"，而
    ``inst_map`` 里那个旧实例仍能被会话偏好解析到。如果只扫"启用的 chat 条目"，这一刻就会
    得出"没人声明不支持"（因为唯一的 offender 被"没启用"过滤掉了）。所以声明扫描宁严勿宽：
    禁用的、非 chat 的条目一起看。代价只是"配置里留着一个禁用且声明不支持的 provider"的
    部署不再享受这项优化（实测本机真实配置里就有这样一条，但它同时还有一条启用的 offender，
    本来也不会命中）。

    残留边界（诚实记录）：本判据假设"被加载的 chat 实例，其 ``id`` 来自配置里的某个条目"。
    若第三方代码绕过 ``ProviderManager`` 直接往 ``inst_map`` 里塞一个配置里没有的实例，
    第 4 条会因为 id 对不上而退回查 DB；但若**同时**把配置里的对应条目删掉、且把那条
    "禁用+改能力声明"的编辑在同一次保存里做完，这份判据看不到那个实例 —— 本插件不改核心、
    也不读 ``inst_map``（私有面），无法再进一步。
    """
    if not isinstance(loaded, (list, tuple)):
        return False  # 不是可数序列就没法比数量（生成器也在此止步）
    if not all_providers_declare_tool_support(loaded):
        return False
    entries = config_provider_configs(config)
    if not entries:
        return False
    if any(_declares_missing_tool_use(entry) for entry in entries):
        return False  # 声明层面的否定信号（含禁用的实例，见 docstring 第 2 条）
    chat_configs = config_chat_provider_configs(config)
    if not chat_configs:
        return False
    loaded_ids = provider_ids(loaded)
    if loaded_ids is None or len(loaded_ids) != len(loaded):
        return False  # 有实例读不到 id，或 id 重复 → 两边无法一一对应
    return loaded_ids == {entry["id"] for entry in chat_configs}


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
        self._warned_non_standard_tool_set = False
        #: "未知原因"（DEBUG）与"异常路径"（ERROR）各自的**有界**去重集合：
        #: 每个原因只报一次，集合到顶后不再放行新原因 → 日志条数也有上界。
        #: 分成两个是为了别让 DEBUG 噪音把 ERROR 的预算吃掉。
        self._unknown_logged = _OnceLog(_UNKNOWN_LOG_CAP)
        self._error_logged = _OnceLog(_UNKNOWN_LOG_CAP)
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
            # 最后一道兜底：走到这里说明 _ensure_tool 里有个没被就地处理的 bug。同一原因
            # 只报一次 —— 否则"每个请求一条带 traceback 的 ERROR"就会从这里复活，而
            # CHANGELOG 明确承诺过没有这种路径。
            self._log_tool_failure_once(exc)

    async def _ensure_tool(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        self._judged_provider = None  # 清零：见 __init__ 里关于本次请求标签的说明
        if not self.enabled:  # 关闭时彻底不介入，痕迹只有启动那行"启用=False"
            return

        tool = self._resolve_tool()
        if tool is None:
            return  # 原因与补救办法已由 _resolve_tool 打过一次

        tool_set = req.func_tool
        if tool_set is not None:
            # 非标准 ``func_tool``（别的插件换成了缺 get_tool/add_tool 的对象）：0.2.0 会在
            # 这里 AttributeError，被钩子外层吞成**每请求一条 ERROR + traceback**。
            # 现在改为：一次性 WARNING（说清是谁的形状不对）+ 跳过注入，绝不抛异常。
            get_tool = getattr(tool_set, "get_tool", None)
            add_tool = getattr(tool_set, "add_tool", None)
            if not callable(get_tool) or not callable(add_tool):
                self._warn_non_standard_tool_set_once(tool_set)
                return
            # 形状对、但一调用就抛：0.2.0 也是每请求一条 ERROR。这里就地兜住并只报一次，
            # 异常不冒泡（冒到外层虽然也能吞掉，但那条兜底日志会按请求刷）。
            try:
                existing = get_tool(KB_TOOL_NAME)
            except Exception as exc:  # noqa: BLE001 - 第三方对象的异常类型不可预期
                self._log_tool_failure_once(exc)
                return
            if existing is not None:
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
            # O1 静态预筛：配置里的 chat provider 与已加载的那批**完全对应**、且没人声明
            # "缺 tool_use" 时，无论核心这次解析到哪一个，判定结果都只能是"支持" —— 那次
            # DB 点查（实测约 1.1–1.2 ms）可以整段跳过。预筛证明不了就照旧查一次，绝不改变
            # "判定为不支持"的语义。
            if self._static_prefilter_proves_support(event):
                judged = (
                    "未查询（静态预筛：配置与已加载的 chat provider 一致，"
                    "且均未声明缺 tool_use）"
                )
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
        try:
            tool_set.add_tool(tool)
        except Exception as exc:  # noqa: BLE001 - 第三方对象的异常类型不可预期
            # 与 get_tool 同一类：形状对但会抛。异常不冒泡，同一原因只报一次。
            self._log_tool_failure_once(exc)
            return
        # 日志取值也不假设一定有 ``tools``：func_tool 可能被别的插件换成自定义对象。
        tool_count = len(getattr(tool_set, "tools", ()))
        # 带上"判定对象"：用户按 README 调 DEBUG 排障时，要能一眼看出这次判的是哪个
        # provider/model，而不是只知道"注入了"。没查时不编造，写明原因。
        logger.debug(
            "[KBTool] 注入 %s，工具数=%d，判定对象=%s", KB_TOOL_NAME, tool_count, judged
        )

    def _log_tool_failure_once(self, exc: BaseException) -> None:
        """``func_tool`` 的 ``get_tool``/``add_tool`` 抛异常：**同一原因只报一次** ERROR。

        为什么不能每请求一条（0.2.0 的行为）：这类对象只要还在，每个请求都会抛同一条异常，
        于是"每请求一条带 traceback 的 ERROR"就成了一条真正的刷屏路径 —— 而 CHANGELOG 里
        "本插件没有会无限刷 ERROR 的路径"这句话必须成立。异常本身仍然**不冒泡**（fail-open：
        最坏只是这次不注入）。

        去重键 = 异常类型 + 消息前 80 字：同类型不同原因各留一条（排障时要看到真正的原因），
        长度有上界（键不会无限长），条数也有上界（``_error_logged`` 到顶后不再放行新键）。
        """
        key = f"toolop:{type(exc).__name__}:{str(exc)[:80]}"
        if self._error_logged.claim(key):
            logger.error(
                "[KBTool] 注入工具时异常，已跳过本次注入（同一原因只报一次）: %s",
                exc,
                exc_info=True,
            )
            return
        if self._error_logged.saturated and not self._error_logged.notified:
            self._error_logged.notified = True
            logger.error(
                "[KBTool] 注入异常的原因已达 %d 种，后续新原因不再记录（注入仍会继续尝试）。",
                _UNKNOWN_LOG_CAP,
            )

    def _warn_non_standard_tool_set_once(self, tool_set: object) -> None:
        """非标准 ``func_tool``（缺 ``get_tool``/``add_tool``）：一次性 WARNING + 跳过注入。

        为什么用 WARNING 而不是 DEBUG（二选一，理由在此）：
        - 这是一条"**插件不工作了**"的可见原因 —— 工具不会被注入，而用户能看到的唯一线索
          就是这条日志。DEBUG 在默认 INFO 级别下不可见，等于把故障藏起来（本插件其它
          静默失效路径都被专门修过，见内置工具注册表返回 None 那条 WARNING）。
        - 它不刷屏：**每个插件实例只报一次**（``_warned_non_standard_tool_set``），与
          "每请求一条带 traceback 的 ERROR"（0.2.0 的行为）相比是纯降噪。
        - 文案给不出"用户能照做的下一步"？能给：指出是**别的插件**换了 ``func_tool`` 对象，
          让用户去查最近装的插件；本插件能做的是安全跳过，不是替它修对象。
        """
        if self._warned_non_standard_tool_set:
            return
        self._warned_non_standard_tool_set = True
        logger.warning(
            "[KBTool] 本次请求的 func_tool 是 %s（缺 get_tool/add_tool），不是核心的 ToolSet；"
            "本插件无法安全判重与追加，已跳过注入（本条只提示一次）。"
            "通常意味着另一个插件把 req.func_tool 换成了自定义对象 —— "
            "请检查最近安装/更新的插件。",
            type(tool_set).__name__,
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
            # 用 debug 而非 warning（默认 INFO 下不可见）；同一原因只记一次 —— 数据库锁死
            # 或配置读坏时每个请求都会抛同一句话，逐次打就是"每请求一条 DEBUG"。
            self._log_unknown_once(
                f"kb:{type(exc).__name__}:{str(exc)[:80]}",
                "[KBTool] 判定知识库可用性失败，按可用处理: %s",
                exc,
            )
            return True

    def _static_prefilter_proves_support(self, event: AstrMessageEvent) -> bool:
        """O1 静态预筛的入口：能否**不查 provider** 就断言"本次必然判为支持"。

        ``True`` = 配置与已加载的 chat provider 一致、且都没有声明缺 ``tool_use``，于是无论
        核心这次解析到哪一个，``_provider_tool_support`` 都只会返回 ``True`` —— 那次 DB 点查
        （两次独立跑：median 1.1247 / 1.1975 ms）可以整段跳过。判据本身见
        ``prefilter_proves_support``（含 terminate 窗口的论证）。

        ``False`` = **证明不了**，调用方照旧查一次。这条路径没有任何"判为不支持"的能力：
        最坏结果只是多查一次 DB，所以它不可能放宽 ``respect_provider_tool_support``。

        为什么有 ``selected_provider`` 时直接放弃预筛：会话级选中走
        ``context.get_provider_by_id()``，它能返回 ``inst_map`` 里的**非 chat** provider
        （TTS/Embedding），那类实例不在 ``get_all_providers()`` 的返回里，扫不到也就证明
        不了。这类请求（WebUI/开放 API 显式选模型）本来就少，退回查一次 DB 是安全的。

        为什么要 ``getattr`` 探测：``get_all_providers`` 不是本插件声明下界（4.23.0）之外
        必须存在的私有面 —— 自定义 Context 替身/第三方加载器可能没有它，缺了就跳过预筛。

        为什么还要读一次配置：``get_all_providers()`` 单独用会在 provider 重载的
        ``terminate`` 窗口里"看不见"正要被删掉的那个实例（见 ``prefilter_proves_support``）。
        配置与加载表由不同的代码路径更新，两者 id 集合一致才算证明。这次 ``get_config(umo)``
        在 ``only_when_kb_configured`` 打开时本来就已经付过（``_kb_available`` 读 ``kb_names``
        用的是同一个方法、同一个 umo），关掉时多一次内存查表。
        """
        if selected_provider_id(event) is not None:
            return False
        getter = getattr(self.context, "get_all_providers", None)
        if not callable(getter):
            return False
        try:
            loaded = getter()
        except Exception:  # noqa: BLE001 - 预筛失败只能退回查 DB
            return False
        try:
            config = self.context.get_config(
                umo=getattr(event, "unified_msg_origin", None)
            )
        except Exception:  # noqa: BLE001 - 同上：读不到配置就是"证明不了"
            return False
        return prefilter_proves_support(loaded, config)

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

    def _log_unknown_once(self, key: str, template: str, exc: object) -> None:
        """ "未知原因"的 DEBUG：同一个键只放行一次，键集合与日志条数都有上界。

        键里含 ``str(exc)[:80]``：上游只要给出带可变内容的异常（端口、超时毫秒、umo、URL），
        每次请求都能造出一个新键。``_unknown_logged`` 的上界（``_UNKNOWN_LOG_CAP``）管住内存，
        "到顶后不再放行新键"管住**日志条数**（≤ 上界 + 1 条一次性提示）。这只影响"同一条
        DEBUG 会不会再出现一次"，不影响任何判定结果。
        """
        if self._unknown_logged.claim(key):
            logger.debug(template, exc)
            return
        if self._unknown_logged.saturated and not self._unknown_logged.notified:
            self._unknown_logged.notified = True
            logger.debug(
                "[KBTool] 未判定/失败原因已达 %d 种，后续新原因不再记录（判定与注入不受影响）。",
                _UNKNOWN_LOG_CAP,
            )

    def _assume_supported(self, key: str, reason: str) -> bool:
        """fail-open：按"支持工具"处理，同一个原因只 DEBUG 一次（返回值恒为 True）。

        去重与上界见 ``_log_unknown_once``：同类型不同原因各留一条，超过上界后不再新增日志，
        但**返回值恒为 True**，判定结果与是否记录日志无关。
        """
        self._log_unknown_once(
            f"probe:{key}",
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
