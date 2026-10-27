"""``main.py`` 的行为测试：脱网、不启动 AstrBot、不写任何真实数据目录。

用例 01–10 对应 SYNTHESIS FIX-7 的清单（其中 03 是 FIX-1「会话级知识库」的回归钉），
11–12 是 FIX-1 验收要求的 fail-open 两个变体，13 是"只依赖 ``astrbot.api`` 公开面"的结构钉。
16–30 是 provider 工具能力判定（``respect_provider_tool_support``）的判定表 —— 逐行对齐
``AUDIT-KB.md`` §3.2；其中 29 是"开关关闭 == 0.2.0"的等价性钉子。

所有用例都走**真实入口** ``on_llm_request``（而不是直接调私有方法）：异常吞噬、幂等、日志
这些契约必须与实际运行时经过同一条路径，否则测的是另一段代码。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest
import yaml

import _astrbot_stub as stub

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 允许出现在 ``_conf_schema.json`` 里的 type。这 5 个在声明下界 4.23.0 与生产 4.29.1 的
#: ``DEFAULT_VALUE_MAP`` 里都存在（``dict``/``text`` 等是 4.29 才有的，列入会写出一个
#: "在 4.23 上非法"的 schema）。
SCHEMA_TYPES = {"bool", "string", "int", "float", "list"}


class FakeToolManager:
    """``Context.get_llm_tool_manager()`` 返回值的替身。

    与核心一致：``get_builtin_tool`` 按名返回**同一个缓存实例**（核心用
    ``builtin_func_list`` 按类缓存），所以"注入的就是核心那一个"可以用 ``is`` 断言。
    ``raises`` 用来模拟内置工具注册表不可用；``returns_none`` 模拟"注册表可用、但这个名字
    没登记"（VERIFY-KB-R3 §9 P3-2：实测核心只会抛异常，所以这属**理论**分支，但它一旦发生
    就是零日志静默失效，必须有回归钉）。
    """

    def __init__(
        self, tool, *, raises: Exception | None = None, returns_none: bool = False
    ) -> None:
        self._tool = tool
        self.raises = raises
        self.returns_none = returns_none
        self.calls = 0

    def get_builtin_tool(self, name: str):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.returns_none:
            return None
        assert name == self._tool.name, f"插件请求了意外的工具名: {name}"
        return self._tool


class FakeProvider:
    """``astrbot.core.provider.provider.Provider`` 的最小替身：插件只读 ``provider_config``。

    真身的 ``provider_config`` 由 ``AbstractProvider.__init__`` 赋值
    （``core/provider/provider.py:34``），里面就是 WebUI 那份 provider 配置（含
    ``modalities``）。``provider_config=None`` 用来模拟"拿到的对象**没有** provider 配置"
    —— 例如 ``get_provider_by_id`` 返回了 TTS/Embedding 实例（详见 AUDIT-KB §3.2 第 9 行）。
    """

    def __init__(self, provider_config: dict | None = None) -> None:
        if provider_config is not None:
            self.provider_config = provider_config

    def __repr__(self) -> str:  # 断言失败时的可读输出
        return (
            f"FakeProvider({getattr(self, 'provider_config', '<无 provider_config>')})"
        )


class FakeContext:
    """``Context`` 的替身：知识库判定 + **可选**的 provider 查询能力。

    默认**一个 provider 方法都不提供**：这既是 0.2.0 测试里 ``FakeContext`` 的原貌，也
    正好复刻"Context 完全没有 provider 查询 API"（旧核心 / 自定义调用方 / 契约冒烟的
    ``_StubContext``，见 AUDIT-KB §3.2 第 8 行与 §4.3）。

    ``provider_api`` 选择挂哪些方法。方法是**实例属性**：没选中的就真的不存在，插件的
    ``getattr(context, "...", None)`` 探测才会得到 ``None``（这是版本差异的关键——4.27.5
    之前没有 async 版）：

    - ``"async"`` → ``get_using_provider_async(umo=...)``（**4.27.5+** 才有）
    - ``"sync"`` → ``get_using_provider(umo=...)``（4.23–4.26 的唯一入口）
    - ``"by_id"`` → ``get_provider_by_id(provider_id)``（会话级 ``selected_provider``）
    """

    def __init__(
        self,
        manager,
        *,
        kb_names=None,
        raises: Exception | None = None,
        provider=None,
        provider_api=(),
        providers_by_id: dict | None = None,
        provider_error: BaseException | None = None,
    ) -> None:
        self._manager = manager
        self.kb_names = list(kb_names or [])
        self.raises = raises
        #: 每次 ``get_config`` 收到的 umo，用来钉住"按会话取配置"而不是只读默认配置。
        self.config_umos: list[str | None] = []
        self.provider = provider
        self.providers_by_id = dict(providers_by_id or {})
        #: 设为异常后每次 provider 查询都抛它；``BaseException`` 也行（见 ``_Forbidden``）。
        self.provider_error = provider_error
        #: 每次 provider 查询记一条 ``"<方法>:<参数>"``；"该查/不该查"全靠它断言。
        self.provider_calls: list[str] = []
        self._install_provider_api(provider_api)

    def get_llm_tool_manager(self):
        return self._manager

    def get_config(self, umo: str | None = None):
        self.config_umos.append(umo)
        if self.raises is not None:
            raise self.raises
        return {"kb_names": list(self.kb_names)}

    def _install_provider_api(self, kinds) -> None:
        kinds = set(kinds)

        async def get_using_provider_async(umo=None):
            self.provider_calls.append(f"async:{umo}")
            self._raise_provider_error()
            return self.provider

        def get_using_provider(umo=None):
            self.provider_calls.append(f"sync:{umo}")
            self._raise_provider_error()
            return self.provider

        def get_provider_by_id(provider_id):
            self.provider_calls.append(f"by_id:{provider_id}")
            self._raise_provider_error()
            return self.providers_by_id.get(provider_id)

        for kind, func in (
            ("async", get_using_provider_async),
            ("sync", get_using_provider),
            ("by_id", get_provider_by_id),
        ):
            if kind in kinds:
                setattr(self, func.__name__, func)

    def _raise_provider_error(self) -> None:
        if self.provider_error is not None:
            raise self.provider_error


#: "配置里没有 ``modalities`` 这个键"——必须与 ``None`` 区分开（AUDIT-KB §3.2 第 10 行）。
MISSING = object()


def make_plugin(
    plugin_module,
    *,
    config: dict | None = None,
    kb_names: list[str] | None = None,
    tool_raises: Exception | None = None,
    tool_returns_none: bool = False,
    config_raises: Exception | None = None,
    provider=None,
    provider_api=(),
    providers_by_id: dict | None = None,
    provider_error: BaseException | None = None,
):
    """构造被测插件，返回 ``(plugin, manager, context, tool)``。"""
    tool = stub.FunctionTool(plugin_module.KB_TOOL_NAME)
    manager = FakeToolManager(tool, raises=tool_raises, returns_none=tool_returns_none)
    context = FakeContext(
        manager,
        kb_names=kb_names,
        raises=config_raises,
        provider=provider,
        provider_api=provider_api,
        providers_by_id=providers_by_id,
        provider_error=provider_error,
    )
    plugin = plugin_module.KBSearchToolPlugin(context, config)
    return plugin, manager, context, tool


def make_provider_case(
    plugin_module,
    modalities=MISSING,
    *,
    api=("async", "sync"),
    provider_id="siliconflow/qwen3-vl-30b-a3b",
    model="qwen3-vl-30b-a3b",
    config: dict | None = None,
    **kwargs,
):
    """「当前 provider 有明确能力声明」的插件：知识库已配置，provider 查询 API 齐全。

    ``modalities=MISSING`` 表示配置里**没有**这个键（与 ``None`` 是两种不同的输入）。
    """
    provider_config = {"id": provider_id, "model": model}
    if modalities is not MISSING:
        provider_config["modalities"] = modalities
    return make_plugin(
        plugin_module,
        config=dict(config) if config else None,
        kb_names=["kb1"],
        provider=FakeProvider(provider_config),
        provider_api=api,
        **kwargs,
    )


def messages(logs, level: int) -> list[str]:
    """取出某个级别的日志正文，便于断言"说了什么、说了几次"。"""
    return [record.getMessage() for record in logs if record.levelno == level]


def test_01_injects_the_core_builtin_instance(plugin_module, hook, logs, hook_kwargs):
    """有知识库 → 注入，且注入的就是核心缓存的那个实例；成功注入留一条 DEBUG。"""
    plugin, manager, context, tool = make_plugin(plugin_module, kb_names=["kb1"])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert [t.name for t in req.func_tool.tools] == [plugin_module.KB_TOOL_NAME]
    assert manager.calls == 1, "启动解析一次，请求期应命中缓存"
    assert context.config_umos == ["aiocqhttp:FriendMessage:10086"], "主配置要按会话取"

    # 钩子注册必须用关键字 priority=HOOK_PRIORITY：钉住**关键字名**，不只是值。
    assert hook_kwargs == [{"priority": plugin_module.HOOK_PRIORITY}]

    # 启动只有一行 INFO（不含版本号：核心在同一屏已经打过）；注入成功只在 DEBUG 留痕。
    assert len(messages(logs, logging.INFO)) == 1
    assert "已加载" in messages(logs, logging.INFO)[0]
    injected = messages(logs, logging.DEBUG)
    assert len(injected) == 1, "成功注入必须有一条 DEBUG，否则运行期完全不可观测"
    assert plugin_module.KB_TOOL_NAME in injected[0]
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []


def test_02_no_kb_skips_injection_and_warns_exactly_once(plugin_module, hook, logs):
    """会话级与主配置都没有知识库 → 不注入；告警恰好一次且必须可行动。"""
    plugin, _, context, _ = make_plugin(plugin_module, kb_names=[])

    for _ in range(3):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is None

    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, "同一份日志里只应有一条告警，否则会刷屏"
    assert "only_when_kb_configured" in warnings[0], "告警必须告诉用户怎么自救"
    assert context.config_umos == ["aiocqhttp:FriendMessage:10086"] * 3


def test_03_session_level_kb_ids_inject_even_if_global_kb_names_empty(
    plugin_module, hook, logs, sp
):
    """FIX-1 回归钉：会话级 ``kb_config.kb_ids`` 非空 + 主配置空 → 仍然注入。"""
    sp.session[("aiocqhttp:FriendMessage:10086", "kb_config")] = {"kb_ids": [7]}
    plugin, _, context, tool = make_plugin(plugin_module, kb_names=[])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    # 会话级配置优先时不该去读主配置（与核心 retrieve_knowledge_base 的分支一致）。
    assert context.config_umos == []
    assert messages(logs, logging.WARNING) == []


def test_04_session_level_kb_ids_empty_disables_injection(plugin_module, hook, sp):
    """会话里显式关掉知识库（``kb_ids=[]``）→ 不注入，即使主配置有知识库。"""
    sp.session[("aiocqhttp:FriendMessage:10086", "kb_config")] = {"kb_ids": []}
    plugin, _, _, _ = make_plugin(plugin_module, kb_names=["kb1"])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is None


def test_05_disabled_plugin_does_not_touch_the_request(plugin_module, hook, logs):
    """``enable=false`` → 完全不介入请求，也不重复解析工具。"""
    plugin, manager, _, _ = make_plugin(
        plugin_module, config={"enable": False}, kb_names=["kb1"]
    )

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is None
    assert manager.calls == 1, "关闭后连「取工具」都不该再做"
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []
    assert plugin_module.KB_TOOL_NAME in messages(logs, logging.INFO)[0]


def test_06_creates_a_tool_set_when_request_has_none(plugin_module, hook):
    """``req.func_tool is None``（纯对话没有别的工具）→ 新建 ToolSet 并注入。"""
    plugin, _, _, tool = make_plugin(plugin_module, kb_names=["kb1"])

    req = stub.ProviderRequest(func_tool=None)
    hook(plugin, req)

    assert isinstance(req.func_tool, stub.ToolSet)
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool


def test_07_existing_same_named_tool_is_not_duplicated_or_replaced(plugin_module, hook):
    """核心 agentic 模式已加过同名工具 → 不重复添加，也不替换成我们的实例。"""
    plugin, manager, _, _ = make_plugin(plugin_module, kb_names=["kb1"])
    existing = stub.FunctionTool(plugin_module.KB_TOOL_NAME)
    tool_set = stub.ToolSet()
    tool_set.add_tool(existing)

    req = stub.ProviderRequest(func_tool=tool_set)
    hook(plugin, req)

    assert [t.name for t in req.func_tool.tools] == [plugin_module.KB_TOOL_NAME]
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is existing
    assert manager.calls == 1


def test_08_resolve_failure_is_contained_then_self_heals(plugin_module, hook, logs):
    """取不到内置工具 → 不抛给调用方、只打一条可行动 ERROR；恢复后自动注入（自愈）。"""
    plugin, manager, _, tool = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        tool_raises=KeyError("Builtin tool astr_kb_search is not registered."),
    )

    errors = messages(logs, logging.ERROR)
    assert len(errors) == 1
    assert "4.23" in errors[0], "ERROR 必须告诉用户去确认什么（版本/注册表）"
    assert logs[-1].levelno == logging.ERROR, "故障时最后一条日志应是 ERROR"

    # 运行期仍取不到：不抛、不刷屏、不注入。
    req = stub.ProviderRequest()
    hook(plugin, req)
    assert req.func_tool is None
    assert len(messages(logs, logging.ERROR)) == 1

    # 工具恢复 → 下一个请求自动注入，不需要重载插件。
    manager.raises = None
    req = stub.ProviderRequest()
    hook(plugin, req)
    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool


def test_09_injection_exception_does_not_escape_the_hook(plugin_module, hook, logs):
    """注入过程中抛异常 → 不打断对话，只留一条 ERROR。"""

    class ExplodingToolSet(stub.ToolSet):
        def add_tool(self, tool):
            raise RuntimeError("boom")

    plugin, _, _, _ = make_plugin(plugin_module, kb_names=["kb1"])

    req = stub.ProviderRequest(func_tool=ExplodingToolSet())
    hook(plugin, req)  # 不抛即通过

    errors = messages(logs, logging.ERROR)
    assert len(errors) == 1
    assert "boom" in errors[0]


def test_10_conf_schema_and_metadata_are_valid():
    """``_conf_schema.json`` 类型合法、``metadata.yaml`` 必填字段齐备且无死字段。"""
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema, "schema 为空意味着插件没有任何可配置项"
    for key, item in schema.items():
        assert item["type"] in SCHEMA_TYPES, f"{key} 的 type 非法: {item['type']}"
        assert item.get("description"), f"{key} 缺少 description，面板上会显示为空"
        assert "default" in item, f"{key} 缺少 default"
    # FIX-1：hint 必须与"会话级优先"的新语义一致，不能再写成"仅看 kb_names"。
    assert "会话" in schema["only_when_kb_configured"]["hint"]

    metadata = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    for field in ("name", "desc", "version", "author"):
        assert isinstance(metadata.get(field), str) and metadata[field].strip(), (
            f"metadata.yaml 缺少必填字段 {field}"
        )
    assert "description" not in metadata, "description 是核心从不读取的死字段（FIX-5）"
    assert re.fullmatch(r"\d+\.\d+\.\d+", metadata["version"]), "version 必须是 SemVer"

    # 核心用 packaging 的 SpecifierSet 校验 astrbot_version（star_manager.py 的
    # PLUGIN_VERSION 检查）；这里用同一个解析器，钉住"能被解析"且"真的包含下界"。
    specifiers = pytest.importorskip("packaging.specifiers")
    spec = specifiers.SpecifierSet(metadata["astrbot_version"])
    assert spec.contains("4.23.0"), "声明下界必须包含 4.23.0"
    assert not spec.contains("4.22.3"), "低于下界的 4.22.x 不该被声明为支持"


def test_11_fail_open_when_session_get_raises(plugin_module, hook, sp):
    """会话配置读取抛异常 → 按"有知识库"处理（fail-open），照常注入。"""
    sp.error = RuntimeError("database is locked")
    plugin, _, _, tool = make_plugin(plugin_module, kb_names=[])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool


def test_12_fail_open_when_get_config_raises(plugin_module, hook):
    """主配置读取抛异常 → 同样 fail-open，照常注入。"""
    plugin, _, _, tool = make_plugin(
        plugin_module, kb_names=[], config_raises=RuntimeError("config is broken")
    )

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool


def test_13_plugin_depends_only_on_public_astrbot_api(plugin_module):
    """插件只依赖 ``astrbot.api`` 公开面：替身里没有 ``astrbot.core`` 也能加载并跑通。"""
    assert plugin_module.KB_TOOL_NAME == "astr_kb_search"
    assert "astrbot.core" not in sys.modules, "插件不该把核心私有模块拖进进程"


def test_14_stub_toolset_matches_core_active_semantics():
    """替身 ``ToolSet.add_tool`` 与核心逐条一致：同名的取舍是「优先 active」，不是「一律覆盖」。

    核心实现见 ``core/agent/tool.py:91-108``；这条用例钉的是**替身本身**——插件永远先
    ``get_tool`` 判重、走不到这些分支，所以如果替身把契约说简单了，别的用例就会在假语义
    上通过，而它本该是"核心契约的钉子"。
    """
    tool_set = stub.ToolSet()
    active_tool = stub.FunctionTool("astr_kb_search", active=True)
    inactive_tool = stub.FunctionTool("astr_kb_search", active=False)

    # 1. 新的不 active、旧的 active → 保留旧的（核心：new_active 为假且 existing 为真）
    tool_set.add_tool(active_tool)
    tool_set.add_tool(inactive_tool)
    assert tool_set.get_tool("astr_kb_search") is active_tool
    assert len(tool_set.tools) == 1

    # 2. 换成 active 的新工具 → 覆盖（核心：new_active 为真）
    replacement = stub.FunctionTool("astr_kb_search", active=True)
    tool_set.add_tool(replacement)
    assert tool_set.get_tool("astr_kb_search") is replacement

    # 3. 旧的已不 active → 新的即便不 active 也覆盖（核心：not existing_active 为真）
    tool_set.tools[0].active = False
    tool_set.add_tool(inactive_tool)
    assert tool_set.get_tool("astr_kb_search") is inactive_tool


def test_15_non_dict_session_config_falls_back_to_global(plugin_module, hook, sp):
    """``sp.session_get`` 返回非 dict → 回落主配置，不抛、也不当会话级配置。

    核心的分支条件是 ``if session_config and "kb_ids" in session_config``；插件用
    ``isinstance(session_cfg, dict)`` 做同样的判断。这条钉住"历史数据写坏/别的插件塞了
    奇怪值"时判定既不抛异常，也不会被误当成会话级配置。
    """
    umo = "aiocqhttp:FriendMessage:10086"
    plugin, _, context, tool = make_plugin(plugin_module, kb_names=["kb1"])

    for bad_value in (["x"], "str", 123, {"top_k": 5}):
        sp.session[(umo, "kb_config")] = bad_value
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is not None, f"{bad_value!r} 应回落到主配置并注入"
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool

    # 每次都读了主配置 → 确实走了 kb_names 兜底，而不是被当成会话级配置短路。
    assert context.config_umos == [umo] * 4


# ---------------------------------------------------------------------------
# provider 工具能力判定（``respect_provider_tool_support``，AUDIT-KB §3.2 判定表 16 行）
#
# 判据只有一条：**``modalities`` 是非空 list 且不含 ``tool_use``** 才算"不支持工具"。
# ``None`` / ``[]`` / 缺键 / 非 list / 取不到 provider / 探测抛异常一律 fail-open 注入
# （AUDIT-KB §0.2：4.29 把空值当"未配置，默认支持全部模态"，按否定信号处理会让绝大多数
# 没填"模型能力"的用户直接失去工具）。
# ---------------------------------------------------------------------------


def test_16_skips_injection_when_modalities_excludes_tool_use(
    plugin_module, hook, logs
):
    """§3.2 第 13 行：``modalities`` 非空且不含 ``tool_use`` → 跳过注入 + WARNING 一次。"""
    plugin, _, context, _ = make_provider_case(plugin_module, ["text"])

    for _ in range(3):  # 同一 provider+model：告警只该出现一次
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is None

    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, "同一 provider+model 只告警一次，否则每请求刷屏"
    assert "tool_use" in warnings[0] and "respect_provider_tool_support" in warnings[0]
    assert "siliconflow/qwen3-vl-30b-a3b" in warnings[0], "告警必须指出是哪个 provider"
    assert messages(logs, logging.DEBUG) == [], "跳过时不该有注入日志"
    assert messages(logs, logging.ERROR) == []
    # 仍然每请求都查：用户切了 provider/模型要立刻生效，而不是沿用上次判断。
    assert context.provider_calls == ["async:aiocqhttp:FriendMessage:10086"] * 3

    # 换了模型（同一 provider id）→ 必须再提醒一次：去重键是 provider+model。
    plugin2, _, _, _ = make_provider_case(plugin_module, ["text"], model="qwen3-vl-7b")
    hook(plugin2, stub.ProviderRequest())
    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 2 and "qwen3-vl-7b" in warnings[1]


def test_17_injects_when_modalities_lists_tool_use(plugin_module, hook, logs):
    """§3.2 第 12 行：``modalities`` 含 ``tool_use`` → 正常注入，不多打日志。"""
    plugin, _, context, tool = make_provider_case(plugin_module, ["text", "tool_use"])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []
    debug = messages(logs, logging.DEBUG)
    assert len(debug) == 1, "支持工具是正常路径：只留一条注入 DEBUG"
    assert plugin_module.KB_TOOL_NAME in debug[0]
    assert context.provider_calls == ["async:aiocqhttp:FriendMessage:10086"]


@pytest.mark.parametrize(
    ("modalities", "reason"),
    [
        (MISSING, "缺键"),
        (None, "None"),
        ([], "空 list"),
        ("text", "字符串"),
        ({"tool_use": True}, "dict"),
        (("text", "tool_use"), "tuple"),
        (123, "int"),
    ],
)
def test_18_unknown_or_unconfigured_modalities_fail_open(
    plugin_module, hook, logs, modalities, reason
):
    """§3.2 第 10/11 行：``None`` / ``[]`` / 缺键 / 非 list 一律 fail-open（4.29 语义）。

    ``tuple`` 也在表里：核心只认 ``isinstance(list)``（``tool_loop_agent_runner.py:680-686``），
    tuple 既不会被核心清空，也不能当否定信号 —— 插件同样按"未知"放行。
    """
    plugin, _, _, tool = make_provider_case(plugin_module, modalities)

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None, f"{reason} 不能被当成'不支持工具'"
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []
    debug = messages(logs, logging.DEBUG)
    assert len(debug) == 2, f"{reason}：一条'按支持处理'的 DEBUG + 一条注入 DEBUG"
    assert sum("按支持处理" in message for message in debug) == 1


def test_19_provider_probe_exception_fails_open_with_one_debug(
    plugin_module, hook, logs
):
    """§3.2 第 9 行：查询抛异常 → 注入 + 一次 DEBUG（按原因去重）+ 绝不 ERROR 刷屏。"""
    plugin, _, context, tool = make_provider_case(
        plugin_module, ["text"], provider_error=RuntimeError("database is locked")
    )

    for _ in range(3):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is not None
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool

    assert messages(logs, logging.ERROR) == [], "探测异常必须自己吞掉"
    debug = messages(logs, logging.DEBUG)
    assert sum("按支持处理" in message for message in debug) == 1, (
        "同一原因只 DEBUG 一次"
    )
    assert sum("RuntimeError" in message for message in debug) == 1
    assert len(context.provider_calls) == 3, (
        "每次请求仍会尝试（换 provider 后要立刻生效）"
    )


def test_20_provider_none_fails_open(plugin_module, hook, logs):
    """§3.2 第 8b 行：查询方法存在但返回 ``None``（没有可用 provider）→ 注入 + DEBUG 一次。"""
    plugin, _, context, tool = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        provider=None,
        provider_api=("async", "sync"),
    )

    for _ in range(2):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is not None
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool

    debug = messages(logs, logging.DEBUG)
    assert sum("provider=None" in message for message in debug) == 1
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []
    assert len(context.provider_calls) == 2


@pytest.mark.parametrize("case", ["not_a_chat_provider", "value_error"])
def test_21_non_chat_provider_fails_open(plugin_module, hook, logs, case):
    """§3.2 第 9 行：拿到非聊天 Provider / 查询抛 ``ValueError`` → 一律 fail-open。

    核心 ``Context.get_using_provider`` 在类型不对时**自己抛** ``ValueError``
    （``core/star/context.py:508-512``）；``get_provider_by_id`` 则可能返回 TTS/Embedding
    实例（没有 ``provider_config``）。两条路径都必须落到"未知 → 注入"。
    """
    if case == "not_a_chat_provider":
        plugin, _, _, tool = make_plugin(
            plugin_module,
            kb_names=["kb1"],
            provider=FakeProvider(None),
            provider_api=("async",),
        )
        expected = "provider_config"
    else:
        plugin, _, _, tool = make_plugin(
            plugin_module,
            kb_names=["kb1"],
            provider_api=("async",),
            provider_error=ValueError("该会话来源的对话模型（提供商）的类型不正确"),
        )
        expected = "ValueError"

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    debug = messages(logs, logging.DEBUG)
    assert sum("按支持处理" in message for message in debug) == 1
    assert sum(expected in message for message in debug) == 1
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []


@pytest.mark.parametrize(
    ("modalities", "injected"),
    [(["text"], False), (["text", "tool_use"], True)],
)
def test_22_legacy_context_falls_back_to_sync_getter(
    plugin_module, hook, logs, modalities, injected
):
    """硬约束 1 / §3.2 第 9 行：4.23–4.26 只有同步 ``get_using_provider``，必须回退过去。

    这条钉的是**声明下界 4.23**（``metadata.yaml:8``）：直接 ``await
    context.get_using_provider_async(...)`` 会在 4.23–4.26 上每请求 ``AttributeError``，
    被外层吞掉后插件静默失效。回退是否成功不能只看"注入了"（fail-open 也会注入），
    所以两个变体都断言：同步方法被调用恰好 1 次 **且** 真的读到了 ``modalities``。
    """
    plugin, _, context, tool = make_provider_case(
        plugin_module, modalities, api=("sync",)
    )

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert context.provider_calls == ["sync:aiocqhttp:FriendMessage:10086"], (
        "旧核心只有同步方法：必须走回退，而不是抛 AttributeError 后 fail-open"
    )
    assert (req.func_tool is not None) is injected
    if injected:
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert messages(logs, logging.ERROR) == []
    assert len(messages(logs, logging.WARNING)) == (0 if injected else 1)


def test_22b_no_provider_api_is_a_silent_fail_open(plugin_module, hook, logs):
    """§3.2 第 8 行：Context 完全没有 provider 查询能力 → **静默** fail-open。

    静默是契约而非疏忽：``test_01`` 断言成功注入路径**恰好 1 条** DEBUG；这条路径若也打
    一条"按支持处理"，``test_01`` 就退化成 2 条。契约冒烟的 ``_StubContext`` 同样是这个
    形状（AUDIT-KB §4.3 的 CI 哨兵）。
    """
    plugin, _, context, tool = make_plugin(plugin_module, kb_names=["kb1"])

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert context.provider_calls == []
    debug = messages(logs, logging.DEBUG)
    assert len(debug) == 1 and plugin_module.KB_TOOL_NAME in debug[0]
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []


def test_23_switch_off_never_queries_the_provider(plugin_module, hook, logs):
    """§3.2 第 7 行：``respect_provider_tool_support=false`` → 0.2.0 旧行为（总是注入）。

    两个变体：provider 明确"不支持工具"、provider 查询直接爆炸 —— 开关关闭时都不该被碰
    一下（``provider_calls`` 为空、没有任何新增日志）。
    """
    cases = (
        (FakeProvider({"id": "p", "model": "m", "modalities": ["text"]}), None),
        (None, RuntimeError("开关关闭时不该被调用")),
    )
    for provider, error in cases:
        plugin, _, context, tool = make_plugin(
            plugin_module,
            kb_names=["kb1"],
            config={"respect_provider_tool_support": False},
            provider=provider,
            provider_api=("async", "sync"),
            provider_error=error,
        )
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is not None
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
        assert context.provider_calls == []

    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []
    assert len(messages(logs, logging.DEBUG)) == 2, "两次跑都只有注入那一条 DEBUG"


@pytest.mark.parametrize(
    ("selected", "session_modalities", "selected_modalities", "injected", "calls"),
    [
        # 会话默认 provider 支持工具，但 WebUI 选了"不支持"的那个 → 以会话选择为准，跳过。
        ("p-sel", ["text", "tool_use"], ["text"], False, ["by_id:p-sel"]),
        # 反过来：默认不支持，用户选了支持的那个 → 注入。
        ("p-sel", ["text"], ["text", "tool_use"], True, ["by_id:p-sel"]),
        # selected 指向已删除的 provider → 回落到会话默认 provider（核心同序）。
        ("p-gone", ["text"], None, False, ["by_id:p-gone", "async:umo-1"]),
    ],
)
def test_24_selected_provider_is_what_gets_judged(
    plugin_module,
    hook,
    logs,
    selected,
    session_modalities,
    selected_modalities,
    injected,
    calls,
):
    """§3.2 补充：会话级 ``selected_provider``（WebUI 选模型）优先于会话默认 provider。

    核心 ``_select_provider`` 就是这个顺序（``core/astr_main_agent.py:283-304``）；不镜像
    会在用户切到"不支持工具"的模型时判错对象、或在切到支持工具时误跳过。
    """
    providers_by_id = {}
    if selected_modalities is not None:
        providers_by_id[selected] = FakeProvider(
            {"id": selected, "model": "m-sel", "modalities": selected_modalities}
        )
    plugin, _, context, tool = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        provider=FakeProvider(
            {"id": "p-session", "model": "m-session", "modalities": session_modalities}
        ),
        provider_api=("async", "by_id"),
        providers_by_id=providers_by_id,
    )

    req = stub.ProviderRequest()
    hook(plugin, req, umo="umo-1", extras={"selected_provider": selected})

    assert context.provider_calls == calls
    assert (req.func_tool is not None) is injected
    if injected:
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
        assert messages(logs, logging.WARNING) == []
    else:
        warnings = messages(logs, logging.WARNING)
        assert len(warnings) == 1
        expected_id = selected if selected_modalities is not None else "p-session"
        assert expected_id in warnings[0], "告警必须指出真正被判否的那个 provider"
    assert messages(logs, logging.ERROR) == []


@pytest.mark.parametrize(
    ("image_urls", "modalities", "injected", "fallback_debug"),
    [
        # 图片请求 + 主 provider 不支持 image → 核心可能切 fallback provider，能力未知 → 放行。
        (["http://x/a.png"], ["text"], True, True),
        # 主 provider 能看图 → 不会切 fallback → 判的是它本身 → 跳过。
        (["http://x/a.png"], ["text", "image"], False, False),
        # 没有图片 → 与图片无关 → 正常判否。
        ([], ["text"], False, False),
    ],
)
def test_25_image_requests_fail_open_only_when_fallback_is_possible(
    plugin_module, hook, logs, image_urls, modalities, injected, fallback_debug
):
    """§3.2 第 14 行：图片 fallback 歧义时 fail-open。

    依据核心 ``_provider_supports_modality`` + ``_select_image_chat_provider``
    （``core/astr_main_agent.py:1412-1443``）：只有 ``req.image_urls`` 非空**且**主 provider
    的 ``modalities`` 不含 ``image`` 时才会切到 fallback provider。此时"当前 provider 不支持
    工具"不等于"这次请求用的 provider 不支持工具"，所以按未知放行。
    """
    plugin, _, _, tool = make_provider_case(plugin_module, modalities)

    req = stub.ProviderRequest(image_urls=image_urls)
    hook(plugin, req)

    assert (req.func_tool is not None) is injected
    if injected:
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    debug = messages(logs, logging.DEBUG)
    assert sum("fallback" in message for message in debug) == int(fallback_debug)
    assert len(messages(logs, logging.WARNING)) == (0 if injected else 1)
    assert messages(logs, logging.ERROR) == []


def test_26_no_kb_returns_before_the_provider_probe(plugin_module, hook, logs):
    """顺序钉子（§3.1）：KB 判定在 provider 判定之前 → 无知识库时一次 provider 都不查。"""
    plugin, _, context, _ = make_plugin(
        plugin_module,
        kb_names=[],
        provider=FakeProvider({"id": "p", "model": "m", "modalities": ["text"]}),
        provider_api=("async", "sync"),
    )

    for _ in range(2):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is None

    assert context.provider_calls == [], "最常见的早退路径不该付 provider 查询成本"
    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, "只出既有的 no-KB 告警（一次），不叠加 provider 告警"
    assert "only_when_kb_configured" in warnings[0]
    assert messages(logs, logging.ERROR) == []


def test_27_non_standard_func_tool_keeps_0_2_0_behavior(plugin_module, hook, logs):
    """AUDIT-KB §3.2 第 4 行**未在本轮实现**：非标准 ``func_tool`` 仍是"ERROR 一次 + 不注入"。

    为什么不顺手加防御：本轮硬约束是"开关关闭时逐字节等价 0.2.0"。给这条输入加防御会让
    它从 ERROR 变成 DEBUG（日志可观察行为变了），等价性就不再成立；AUDIT-KB §7 第 6 行也
    把它列为"本轮不修"的残余风险（非公开契约的边缘）。

    本用例的价值是把现状钉成**与开关无关**：两个开关值下行为完全一致，所以等价性成立。
    """
    for switch in (True, False):
        plugin, _, context, _ = make_provider_case(
            plugin_module,
            ["text"],
            config={"respect_provider_tool_support": switch},
        )

        class FunctionToolBag:  # 只有 add_tool，没有 get_tool
            def __init__(self) -> None:
                self.added = []

            def add_tool(self, tool) -> None:
                self.added.append(tool)

        bag = FunctionToolBag()
        req = stub.ProviderRequest(func_tool=bag)
        hook(plugin, req)  # 不抛即通过（异常由外层 hook 吞掉）
        assert bag.added == [], "拿不到 get_tool 就不该往里塞工具"
        assert context.provider_calls == [], (
            "同名判重阶段就失败了，轮不到 provider 判定"
        )

    errors = messages(logs, logging.ERROR)
    assert len(errors) == 2, "每个开关值各一条：异常被外层吞掉并留痕（0.2.0 行为）"
    assert all("get_tool" in message for message in errors)
    assert messages(logs, logging.WARNING) == []


def test_28_conf_schema_and_plugin_read_the_same_config_keys(plugin_module):
    """配置键 1:1：``_conf_schema.json`` 的键 == 插件真正读取的键，默认值一致。

    插件没有配置 dataclass（只有 ``__init__`` 里的属性），所以"1:1"落在这两件事上：
    schema 的键集合是显式的、且**每个键都真的被读进属性**。少一个 = 面板上有个死开关；
    多一个 = 代码读了一个面板里不存在的键。
    """
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert set(schema) == {
        "enable",
        "only_when_kb_configured",
        "respect_provider_tool_support",
    }

    item = schema["respect_provider_tool_support"]
    assert item["type"] == "bool", "非 bool 会让面板渲染成输入框"
    assert item["default"] is True, "默认必须是 true：false 等于没修（AUDIT-KB §3.4）"
    assert item["description"]
    hint = item["hint"]
    assert "tool_use" in hint, "hint 必须写清判据里的关键词"
    assert "非空" in hint, "hint 必须写清'非空 list'"
    assert "放行" in hint, "hint 必须写清未知/空值一律放行（fail-open）"
    assert "respect_provider_tool_support" in hint, "hint 要告诉用户关闭后回到旧行为"

    # 默认值：三个键都从默认配置读出 True。
    plugin, _, _, _ = make_plugin(plugin_module)
    assert plugin.enabled is True
    assert plugin.only_when_kb_configured is True
    assert plugin.respect_provider_tool_support is True

    # 每个键都真的被读取（不是死字段）。
    plugin, _, _, _ = make_plugin(
        plugin_module,
        config={
            "enable": False,
            "only_when_kb_configured": False,
            "respect_provider_tool_support": False,
        },
    )
    assert plugin.enabled is False
    assert plugin.only_when_kb_configured is False
    assert plugin.respect_provider_tool_support is False


#: 开关关闭时若碰了 provider 就会抛出它。
#:
#: 刻意继承 ``BaseException``：插件的 ``except Exception``（provider 探测）与钩子的
#: ``except Exception``（兜底）都拦不住 —— 一旦误触，用例会以"未捕获异常"失败，而不是被
#: fail-open 悄悄吞掉后"看起来通过"。
class _ProviderMustNotBeTouched(BaseException):
    pass


@pytest.mark.parametrize(
    "modalities",
    [MISSING, None, [], ["text"], ["text", "tool_use"], "text", {"x": 1}],
)
@pytest.mark.parametrize("kb_names", [["kb1"], []])
def test_29_switch_off_is_observably_identical_to_0_2_0(
    plugin_module, hook, logs, modalities, kb_names
):
    """开关关闭 → provider 状态对结果**零影响**、零新增日志 = 逐字节等价 0.2.0。

    做法：把 provider 查询装成"一碰就抛 ``BaseException``"的陷阱。0.2.0 的 ``_ensure_tool``
    从不查 provider，所以它在这张矩阵（全部 modalities 形态 × 有无知识库）上应当一次都不
    触发；用例同时钉住注入结果与日志级别/条数/文案——等价性里"可观察行为"的部分。
    """
    provider_config = {"id": "p", "model": "m"}
    if modalities is not MISSING:
        provider_config["modalities"] = modalities
    plugin, _, context, tool = make_plugin(
        plugin_module,
        kb_names=kb_names,
        config={"respect_provider_tool_support": False},
        provider=FakeProvider(provider_config),
        provider_api=("async", "sync", "by_id"),
        provider_error=_ProviderMustNotBeTouched("开关关闭时不该查询 provider"),
    )

    req = stub.ProviderRequest()
    hook(plugin, req)  # 触发陷阱即失败（两处 except Exception 都拦不住）

    assert context.provider_calls == []
    if kb_names:
        assert req.func_tool is not None
        assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
        assert messages(logs, logging.DEBUG) == [
            f"[KBTool] 注入 {plugin_module.KB_TOOL_NAME}，工具数=1"
        ]
        assert messages(logs, logging.WARNING) == []
    else:
        assert req.func_tool is None
        assert messages(logs, logging.DEBUG) == []
        assert len(messages(logs, logging.WARNING)) == 1
    assert messages(logs, logging.ERROR) == []


def test_30_only_when_kb_configured_off_injects_without_kb(plugin_module, hook, logs):
    """§3.2 第 6 行（+ 第 13 行叠加）：关掉知识库闸门时无知识库也注入，但 provider 闸门照旧。

    两个开关是**独立**的：`only_when_kb_configured=false` 让"没有知识库"不再阻止注入，
    而 `respect_provider_tool_support` 仍然在 provider 明确不支持工具时拦下这次注入。
    """
    plugin, _, _, tool = make_plugin(
        plugin_module,
        config={"only_when_kb_configured": False},
        kb_names=[],
        provider=FakeProvider(
            {"id": "p", "model": "m", "modalities": ["text", "tool_use"]}
        ),
        provider_api=("async",),
    )
    req = stub.ProviderRequest()
    hook(plugin, req)
    assert req.func_tool is not None, "关掉知识库闸门后应立即注入"
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert messages(logs, logging.WARNING) == [], "没有知识库告警：闸门已关"

    # 同一份配置，provider 明确不支持工具 → 仍然跳过（知识库闸门关不代表 provider 闸门关）。
    plugin2, _, _, _ = make_plugin(
        plugin_module,
        config={"only_when_kb_configured": False},
        kb_names=[],
        provider=FakeProvider({"id": "p2", "model": "m", "modalities": ["text"]}),
        provider_api=("async",),
    )
    req2 = stub.ProviderRequest()
    hook(plugin2, req2)
    assert req2.func_tool is None
    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1 and "tool_use" in warnings[0]
    assert messages(logs, logging.ERROR) == []


# ---------------------------------------------------------------------------
# 配置值健壮性（P2-1，R4 必修）
#
# 实测的反例（VERIFY-KB-R3 §9 P2-1）：手改 ``_conf_schema.json`` 里
# ``respect_provider_tool_support: "false"`` 时 ``bool("false") is True`` → 闸门**仍然开**，
# 用户的意图被静默反转；键名拼错则整项被静默忽略、用户以为生效了。
# 修法：布尔值走显式白名单，认不出就用默认值 + 一次性 WARNING；未知键同样一次性 WARNING。
# ---------------------------------------------------------------------------

#: 三个布尔配置键 → ``(插件属性名, 默认值)``（与 ``_conf_schema.json`` 一致）。
#: 注意 ``enable`` 这个键落到的属性叫 ``enabled``（0.1.0 起的历史命名，改名会破坏兼容）。
BOOL_SETTINGS = {
    "enable": ("enabled", True),
    "only_when_kb_configured": ("only_when_kb_configured", True),
    "respect_provider_tool_support": ("respect_provider_tool_support", True),
}

#: 白名单：这些写法必须**逐字节**等价于对应的真 bool。
BOOL_TOKENS = [
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("1", True),
    ("0", False),
    ("true", True),
    ("false", False),
    ("TRUE", True),
    ("False", False),
    (" true ", True),
    ("yes", True),
    ("no", False),
    ("on", True),
    ("off", False),
]


@pytest.mark.parametrize("key", sorted(BOOL_SETTINGS))
@pytest.mark.parametrize(("value", "expected"), BOOL_TOKENS)
def test_32_bool_settings_accept_the_explicit_whitelist(
    plugin_module, logs, key, value, expected
):
    """白名单内的写法（含真 bool）都按值解析，且**不**产生告警。

    真 bool 的路径必须与 0.2.0 逐字节相同：``test_23``/``test_29`` 的等价性正是建立在
    "真 bool 不做任何额外动作"上。
    """
    attr, _ = BOOL_SETTINGS[key]
    plugin, _, _, _ = make_plugin(plugin_module, config={key: value})

    assert getattr(plugin, attr) is expected
    assert messages(logs, logging.WARNING) == [], "白名单内的值不该告警"
    assert messages(logs, logging.ERROR) == []


@pytest.mark.parametrize("key", sorted(BOOL_SETTINGS))
@pytest.mark.parametrize(
    "value", ["FALSE!", "maybe", "2", 2, -1, 1.5, None, [], {"enabled": False}, ""]
)
def test_33_unrecognized_bool_value_uses_the_default_with_one_warning(
    plugin_module, logs, key, value
):
    """认不出的值 → 用**默认值**（不是"真值"），并一次性 WARNING 说清后果。

    ``None``/``""``/``2``/``1.5``/容器都在表里：它们都能被 ``bool()`` 悄悄变成 True 或
    False，而用户真正想说的值不可知 —— 这时唯一安全的做法是回到默认值并明说。
    """
    attr, default = BOOL_SETTINGS[key]
    plugin, _, _, _ = make_plugin(plugin_module, config={key: value})

    assert getattr(plugin, attr) is default
    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, f"{value!r} 应恰好一条告警，实际 {warnings}"
    assert key in warnings[0], "告警必须指出是哪个键"
    assert "无法识别" in warnings[0], "告警必须说明原因，而不是静默"
    assert str(default) in warnings[0], "告警必须说明实际用了哪个默认值"
    assert messages(logs, logging.ERROR) == []


def test_33b_unrecognized_bool_warns_once_per_plugin_not_per_request(
    plugin_module, hook, logs
):
    """告警只出现在加载期一次：连续 3 个请求不再重复（否则就是刷屏）。"""
    plugin, _, _, _ = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        config={"respect_provider_tool_support": "maybe"},
        provider=FakeProvider({"id": "p", "model": "m", "modalities": ["text"]}),
        provider_api=("async",),
    )

    for _ in range(3):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is None, "取值失败后回落默认 True，闸门照常生效"

    config_warnings = [
        message for message in messages(logs, logging.WARNING) if "无法识别" in message
    ]
    assert len(config_warnings) == 1, f"只该有加载期那一条，实际 {config_warnings}"
    # 闸门本身仍然按默认 True 生效，所以另有 1 条「未勾 tool_use」的 provider 告警
    # （它按 provider+model 去重，3 次请求只 1 条）—— 两类告警互不干扰。
    assert len(messages(logs, logging.WARNING)) == 2
    assert messages(logs, logging.ERROR) == []
    assert plugin.respect_provider_tool_support is True


@pytest.mark.parametrize("token", ["false", "0", "no", "off", "FALSE"])
def test_34_string_false_really_closes_the_provider_gate(
    plugin_module, hook, logs, token
):
    """P2-1 的**用户可见**修复：字符串 ``"false"`` 必须真的关掉闸门。

    做法与 ``test_29`` 相同：把 provider 查询装成"一碰就抛 ``BaseException``"的陷阱。
    修复前的 ``bool("false")`` 为 True → 陷阱会被踩中；修复后一次都不该被调用。
    """
    plugin, _, context, tool = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        config={"respect_provider_tool_support": token},
        provider=FakeProvider({"id": "p", "model": "m", "modalities": ["text"]}),
        provider_api=("async", "sync", "by_id"),
        provider_error=_ProviderMustNotBeTouched("字符串 false 必须关闭闸门"),
    )

    req = stub.ProviderRequest()
    hook(plugin, req)

    assert plugin.respect_provider_tool_support is False
    assert context.provider_calls == [], "闸门已关：一次 provider 查询都不该发生"
    assert req.func_tool is not None, "闸门关闭 → 回到 0.2.0 的「总是注入」"
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
    assert messages(logs, logging.WARNING) == []
    assert messages(logs, logging.ERROR) == []


def test_35_unknown_config_key_warns_once_and_is_ignored(plugin_module, hook, logs):
    """拼错的键名（``respect_provider_tools_support``）→ 一次性 WARNING，不静默吞掉。

    这是 P2-1 的另一半：用户以为自己改的是闸门，实际写了个不存在的键。没有 schema 校验的
    情况下，唯一能让人发现这件事的就是一条把**拼错的名字**和**可用键**都列出来的告警。
    """
    typo = "respect_provider_tools_support"  # tool 后面多了一个 s
    plugin, _, _, _ = make_plugin(plugin_module, kb_names=["kb1"], config={typo: False})

    assert plugin.respect_provider_tool_support is True, "拼错的键不该生效"
    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, f"未知键应恰好一条告警，实际 {warnings}"
    assert typo in warnings[0], "告警必须列出拼错的那个键"
    assert "respect_provider_tool_support" in warnings[0], "告警要给出可用键"
    assert "enable" in warnings[0] and "only_when_kb_configured" in warnings[0]

    for _ in range(3):  # 请求期不再重复
        hook(plugin, stub.ProviderRequest())
    assert len(messages(logs, logging.WARNING)) == 1
    assert messages(logs, logging.ERROR) == []


def test_35b_unknown_config_keys_are_listed_in_one_warning(plugin_module, logs):
    """多个未知键合成**一条**告警（键名全部列出），不是一个键一条。"""
    # 构造插件这一步本身就会触发告警（告警在 __init__ 里，不依赖任何请求）。
    make_plugin(
        plugin_module,
        config={"enable": True, "enalbe": False, "only_when_kb_configured": "maybe"},
    )

    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 2, "一条未知键 + 一条取值无法识别（各自一次）"
    unknown = [message for message in warnings if "enalbe" in message]
    assert len(unknown) == 1
    assert messages(logs, logging.ERROR) == []


# ---------------------------------------------------------------------------
# 其余 P3 收口（VERIFY-KB-R3 §9）
# ---------------------------------------------------------------------------


def test_36_registry_returning_none_warns_once_and_self_heals(
    plugin_module, hook, logs
):
    """P3-2：``get_builtin_tool`` 返回 ``None``（不是抛异常）→ 一次性 WARNING，不静默失效。

    与"抛异常"分支（``test_08``）区分开：那条是 ERROR 且文案指向版本/注册表，这条是
    "名字没登记"的 WARNING —— 两种原因的修法不同，日志不能混成一句。
    """
    plugin, manager, _, tool = make_plugin(
        plugin_module, kb_names=["kb1"], tool_returns_none=True
    )

    warnings = messages(logs, logging.WARNING)
    assert len(warnings) == 1, f"返回 None 必须有且只有一条 WARNING，实际 {warnings}"
    assert plugin_module.KB_TOOL_NAME in warnings[0]
    assert "None" in warnings[0], "文案要点明是返回 None，而不是异常"
    assert messages(logs, logging.ERROR) == [], "返回 None 不是异常，不该用 ERROR"

    # 请求期：不注入、也不再重复告警（每个请求都会重试，重试不能刷屏）。
    for _ in range(2):
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is None
    assert len(messages(logs, logging.WARNING)) == 1
    assert manager.calls == 3, "启动一次 + 每次请求各重试一次（自愈的前提）"

    # 注册表恢复 → 下一个请求自动注入，不需要重载插件。
    manager.returns_none = False
    req = stub.ProviderRequest()
    hook(plugin, req)
    assert req.func_tool is not None
    assert req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool


def test_37_exception_dedupe_uses_the_message_summary(plugin_module, hook, logs):
    """P3-1：异常去重键 = 类型 + 消息摘要，同类型不同原因**各留一条**。

    实测（VERIFY-KB-R3 §7 表「同一异常类型、两种原因」）：只按类型去重时第二条被静默吞掉，
    排障时看不到真正的原因。同一句话重复出现仍只记一条（去重本身要保住）。
    """
    plugin, _, context, tool = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        provider_api=("async",),
        provider_error=RuntimeError("first reason"),
    )

    def run_once() -> None:
        req = stub.ProviderRequest()
        hook(plugin, req)
        assert req.func_tool is not None and (
            req.func_tool.get_tool(plugin_module.KB_TOOL_NAME) is tool
        )

    run_once()
    context.provider_error = RuntimeError("second reason")
    run_once()
    run_once()  # 同一句话重复：不该再记

    debug = [
        message for message in messages(logs, logging.DEBUG) if "按支持处理" in message
    ]
    assert len(debug) == 2, f"两种原因各一条，实际 {debug}"
    assert any("first reason" in message for message in debug)
    assert any("second reason" in message for message in debug)
    assert messages(logs, logging.ERROR) == [], "探测异常必须自己吞掉"


def test_37b_exception_summary_is_truncated_to_80_chars(plugin_module, hook, logs):
    """超长异常消息只取前 80 字进去重键：避免键无限长，也避免截断后两条撞在一起。"""
    long_a = "x" * 200 + "A"
    long_b = "x" * 200 + "B"  # 前 80 字与 long_a 相同 → 视为同一原因（去重）

    plugin, _, context, _ = make_plugin(
        plugin_module,
        kb_names=["kb1"],
        provider_api=("async",),
        provider_error=RuntimeError(long_a),
    )
    hook(plugin, stub.ProviderRequest())
    context.provider_error = RuntimeError(long_b)
    hook(plugin, stub.ProviderRequest())

    debug = [
        message for message in messages(logs, logging.DEBUG) if "按支持处理" in message
    ]
    assert len(debug) == 1, "前 80 字相同时应视为同一原因（键不能无限增长）"
