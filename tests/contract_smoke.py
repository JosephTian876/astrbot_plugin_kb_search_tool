"""契约冒烟：在**真实 AstrBot 源码**上验证插件对核心的每一条假设。

默认不跑 —— 普通 ``pytest -q`` 在收集阶段就会跳过它（缺 ``ASTRBOT_SRC``），既不拖慢
也不污染那套脱网测试。要跑它需要一份 AstrBot 源码树（**任何** AstrBot 源码树都行，
不需要是当前生产版本；``ASTRBOT_SRC`` 指到的是**包含** ``astrbot/`` 包的那一层目录）：

```powershell
# 4.29.1（生产源码，只读）
$env:ASTRBOT_SRC = '<AstrBot 源码树>'          # 例如 …/backend/app
& python -m pytest tests/contract_smoke.py -q

# 4.23.0（声明下界；git archive v4.23.0 astrbot | tar -x 导出的树）
$env:ASTRBOT_SRC = '<导出目录>'
& python -m pytest tests/contract_smoke.py -q
```

**这个"绿"必须是有意义的**：父测试会把子进程输出里的 evidence JSON 抠出来，断言
``astrbot.__file__`` 反查出的包路径**确实位于 ``ASTRBOT_SRC`` 之下**（并把
``provider_api`` / ``provider_gate`` 也核对一遍）。否则 ``ASTRBOT_SRC`` 指错目录时
子进程会静默回落到 venv 里已安装的 AstrBot，测试仍报绿——那是一个证明不了任何事的绿。

为什么要走子进程：pytest 进程的 ``conftest`` 已经把 ``astrbot`` 换成了替身（写进
``sys.modules``），在本进程里再 import 真实核心只会拿到替身。子进程从零导入，才是
"真实核心"。它只往临时目录写（``ASTRBOT_ROOT`` = ``tempfile.mkdtemp()``），不碰任何
真实数据目录。

断言（对应 round2/adversarial.md §3 的"不要改坏"清单）：
1. ``from astrbot.api import FunctionTool, ToolSet, logger, sp`` 与
   ``from astrbot.api.provider import ProviderRequest`` 都成立；
2. 导入 ``main.py`` 后 ``star_map[<module>]`` 有条目，且 ``star_cls_type`` 就是插件类
   （删掉 ``@register`` 之后仍被核心识别的回归钉）；
3. 钩子注册恰好 1 条、``priority == HOOK_PRIORITY``；
4. 真实 ``FunctionToolManager`` + 真实 ``ProviderRequest`` 跑一次钩子 → 注入成功，
   且注入的就是核心缓存的那个实例；
5. 插件模块导入期不产生 ``DeprecationWarning``；
6. **版本差异**：真实 ``Context`` 到底有哪些 provider 查询方法（``get_using_provider_async``
   是 4.27.5 才出现的），并把这个事实记进 evidence；
7. **注入前判定**：用一个"与真实 ``Context`` 同构"的 shim（4.23–4.26 上没有 async 方法）跑
   ``modalities=["text"]`` → 跳过、``modalities=None`` → 放行。插件若硬 ``await`` 一个不存在的
   方法就会 fail-open 静默注入，第 7 条的"应跳过"断言会当场变红 —— 这就是 4.23 下界钉子。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _contract_evidence import assert_evidence_matches, extract_evidence

REPO_ROOT = Path(__file__).resolve().parent.parent
ASTRBOT_SRC = os.environ.get("ASTRBOT_SRC", "").strip()

if not ASTRBOT_SRC and __name__ != "__main__":
    pytest.skip(
        "未设置 ASTRBOT_SRC：跳过真实核心契约冒烟（用法见 tests/contract_smoke.py 顶部）",
        allow_module_level=True,
    )


class _StubContext:
    """只实现插件真正调用的两个方法；核心侧对象都是真的。"""

    def __init__(self, manager, config):
        self._manager = manager
        self._config = config

    def get_llm_tool_manager(self):
        return self._manager

    def get_config(self, umo=None):
        return dict(self._config)


class _Event:
    """插件只读 ``unified_msg_origin``；用真实 event 需要平台适配器，这里不需要。"""

    def __init__(self, umo: str) -> None:
        self.unified_msg_origin = umo


def _probe() -> dict[str, object]:
    """在子进程里跑真实核心：任何一条断言失败都会让进程非零退出。"""
    import asyncio
    import importlib.util
    import warnings

    sys.path.insert(0, ASTRBOT_SRC)
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="kbtool_contract_")

    import astrbot

    # 1. 公开面：这四件套 + ProviderRequest 是插件全部的依赖，缺一个就该红。
    from astrbot.api import FunctionTool, ToolSet, logger, sp
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.provider.func_tool_manager import FunctionToolManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    evidence: dict[str, object] = {
        "astrbot_package": str(Path(astrbot.__file__).resolve().parent),
        "ToolSet": f"{ToolSet.__module__}.{ToolSet.__qualname__}",
        "FunctionTool": f"{FunctionTool.__module__}.{FunctionTool.__qualname__}",
        "ProviderRequest": (
            f"{ProviderRequest.__module__}.{ProviderRequest.__qualname__}"
        ),
        "logger": type(logger).__name__,
        "sp": type(sp).__name__,
    }

    # 2. 导入插件（并在这一小段里收集告警）
    spec = importlib.util.spec_from_file_location(
        "kbtool_contract_plugin", REPO_ROOT / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spec.loader.exec_module(module)

    deprecations = [
        f"{w.category.__name__}: {w.message}"
        for w in caught
        if issubclass(w.category, DeprecationWarning)
    ]
    assert deprecations == [], f"插件导入期不该有 DeprecationWarning: {deprecations}"

    # 3. 核心识别（删掉 @register 后靠 Star.__init_subclass__ 自动登记）
    metadata = star_map.get(module.__name__)
    assert metadata is not None, f"star_map 里没有 {module.__name__}"
    assert metadata.star_cls_type is module.KBSearchToolPlugin
    evidence["star_map_entry"] = module.__name__

    # 4. 钩子注册
    handlers = [
        handler
        for handler in star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent, only_activated=False
        )
        if module.__name__ in handler.handler_full_name
    ]
    assert len(handlers) == 1, f"钩子应恰好注册 1 条，实际 {len(handlers)}"
    assert handlers[0].extras_configs.get("priority") == module.HOOK_PRIORITY
    evidence["hook"] = [handlers[0].handler_full_name, module.HOOK_PRIORITY]

    # 5. 真实对象驱动一次注入
    manager = FunctionToolManager()
    builtin_tool = manager.get_builtin_tool(module.KB_TOOL_NAME)
    plugin = module.KBSearchToolPlugin(
        _StubContext(manager, {"kb_names": ["CONTRACT_KB"]})
    )
    request = ProviderRequest(prompt="hi")
    assert request.func_tool is None, "ProviderRequest 默认应无工具集"
    asyncio.run(plugin.on_llm_request(_Event("contract:FriendMessage:1"), request))

    assert request.func_tool is not None, "钩子没有注入工具集"
    assert request.func_tool.get_tool(module.KB_TOOL_NAME) is builtin_tool, (
        "注入的必须就是核心缓存的那个实例"
    )
    evidence["injected"] = [tool.name for tool in request.func_tool.tools]
    evidence["injected_is_core_instance"] = True

    # 6. 版本差异：真实 Context 到底有哪些 provider 查询方法。
    #    ``get_using_provider_async`` 是 4.27.5 才出现的；4.23–4.26 只有同步版
    #    （4.23.0：core/star/context.py:359）。这条决定插件该走哪条分支。
    from astrbot.api.star import Context

    provider_api = {
        "async": hasattr(Context, "get_using_provider_async"),
        "sync": hasattr(Context, "get_using_provider"),
        "by_id": hasattr(Context, "get_provider_by_id"),
    }
    evidence["provider_api"] = provider_api
    assert provider_api["sync"], (
        "Context 必须有同步 get_using_provider（4.23–4.26 唯一入口）"
    )

    # 7. 用"与真实 Context 同构"的 shim 跑注入前判定：插件只能依赖真实存在的方法。
    #    旧版本上没有 async 版 → 把 async 方法从 shim 上摘掉，逼插件走同步回退；插件若硬
    #    await 一个不存在的方法会 fail-open 静默注入，下面的"应跳过"断言就会红。
    class _FakeProvider:
        """插件只读 ``provider_config``（真身见 core/provider/provider.py:34）。"""

        def __init__(self, provider_config):
            self.provider_config = provider_config

    class _ProviderShim(_StubContext):
        """只暴露真实 ``Context`` 真的有的方法；provider 本身是假的。"""

        def __init__(self, manager, config, provider_config):
            super().__init__(manager, config)
            self._provider = _FakeProvider(provider_config)

        async def get_using_provider_async(self, umo=None):
            return self._provider

        def get_using_provider(self, umo=None):
            return self._provider

    if not provider_api["async"]:
        delattr(_ProviderShim, "get_using_provider_async")  # 复刻 4.23–4.26 的形状

    unsupported = ProviderRequest(prompt="hi")
    asyncio.run(
        module.KBSearchToolPlugin(
            _ProviderShim(
                manager,
                {"kb_names": ["CONTRACT_KB"]},
                {"id": "contract/unsupported", "model": "m", "modalities": ["text"]},
            )
        ).on_llm_request(_Event("contract:FriendMessage:2"), unsupported)
    )
    assert unsupported.func_tool is None, "modalities=['text'] 应跳过注入"

    unconfigured = ProviderRequest(prompt="hi")
    asyncio.run(
        module.KBSearchToolPlugin(
            _ProviderShim(
                manager,
                {"kb_names": ["CONTRACT_KB"]},
                {"id": "contract/unset", "model": "m", "modalities": None},
            )
        ).on_llm_request(_Event("contract:FriendMessage:3"), unconfigured)
    )
    assert unconfigured.func_tool is not None, "modalities=None 必须 fail-open 注入"

    evidence["provider_gate"] = {
        "modalities_text": "skip",
        "modalities_none": "inject",
        "via": "async" if provider_api["async"] else "sync",
    }
    return evidence


def test_contract_against_real_astrbot_core() -> None:
    """默认 skip；设置 ``ASTRBOT_SRC`` 后在子进程里跑真实核心契约冒烟。"""
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        # Windows 控制台默认 GBK：显式让子进程按 UTF-8 输出（它要打印中文 JSON），
        # 父进程再按 UTF-8 解，避免 reader 线程解码失败导致 stdout 变成 None。
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=180,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, (
        f"真实核心契约冒烟失败（ASTRBOT_SRC={ASTRBOT_SRC}）\n{output}"
    )
    assert "CONTRACT_OK" in output, f"子进程没有打出 CONTRACT_OK\n{output}"

    # 关键一步：退出码 0 + CONTRACT_OK **不足以**说明测到了你指定的树。子进程做的是
    # ``sys.path.insert(0, ASTRBOT_SRC)`` 后 ``import astrbot``，ASTRBOT_SRC 指错目录时会
    # 静默回落到 venv 里已安装的 AstrBot（实测：指向空目录仍 1 passed）。这里用
    # ``astrbot.__file__`` 反查包路径，要求它确实落在 ASTRBOT_SRC 之下。
    evidence = extract_evidence(output)
    assert_evidence_matches(evidence, ASTRBOT_SRC)


if __name__ == "__main__":
    print(json.dumps(_probe(), ensure_ascii=False, indent=2))
    print("CONTRACT_OK")
