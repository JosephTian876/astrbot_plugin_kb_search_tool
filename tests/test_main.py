"""``main.py`` 的行为测试：脱网、不启动 AstrBot、不写任何真实数据目录。

用例 01–10 对应 SYNTHESIS FIX-7 的清单（其中 03 是 FIX-1「会话级知识库」的回归钉），
11–12 是 FIX-1 验收要求的 fail-open 两个变体，13 是"只依赖 ``astrbot.api`` 公开面"的结构钉。

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
    ``raises`` 用来模拟内置工具注册表不可用。
    """

    def __init__(self, tool, *, raises: Exception | None = None) -> None:
        self._tool = tool
        self.raises = raises
        self.calls = 0

    def get_builtin_tool(self, name: str):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        assert name == self._tool.name, f"插件请求了意外的工具名: {name}"
        return self._tool


class FakeContext:
    """``Context`` 的替身：只实现插件真正调用的两个方法。"""

    def __init__(
        self, manager, *, kb_names=None, raises: Exception | None = None
    ) -> None:
        self._manager = manager
        self.kb_names = list(kb_names or [])
        self.raises = raises
        #: 每次 ``get_config`` 收到的 umo，用来钉住"按会话取配置"而不是只读默认配置。
        self.config_umos: list[str | None] = []

    def get_llm_tool_manager(self):
        return self._manager

    def get_config(self, umo: str | None = None):
        self.config_umos.append(umo)
        if self.raises is not None:
            raise self.raises
        return {"kb_names": list(self.kb_names)}


def make_plugin(
    plugin_module,
    *,
    config: dict | None = None,
    kb_names: list[str] | None = None,
    tool_raises: Exception | None = None,
    config_raises: Exception | None = None,
):
    """构造被测插件，返回 ``(plugin, manager, context, tool)``。"""
    tool = stub.FunctionTool(plugin_module.KB_TOOL_NAME)
    manager = FakeToolManager(tool, raises=tool_raises)
    context = FakeContext(manager, kb_names=kb_names, raises=config_raises)
    plugin = plugin_module.KBSearchToolPlugin(context, config)
    return plugin, manager, context, tool


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
