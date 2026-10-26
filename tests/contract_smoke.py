"""契约冒烟：在**真实 AstrBot 源码**上验证插件对核心的每一条假设。

默认不跑 —— 普通 ``pytest -q`` 在收集阶段就会跳过它（缺 ``ASTRBOT_SRC``），既不拖慢
也不污染那套脱网测试。要跑它需要一份 AstrBot 源码树：

```powershell
$py = 'D:\\UwU\\Documents\\dshworkdir\\AstrBot\\.venv\\Scripts\\python.exe'

# 4.29.1（生产源码，只读）
$env:ASTRBOT_SRC = 'D:\\AstrBot\\backend\\app'
& $py -m pytest tests/contract_smoke.py -q

# 4.23.0（声明下界；git archive v4.23.0 astrbot | tar -x 导出的树）
$env:ASTRBOT_SRC = '<导出目录>'
& $py -m pytest tests/contract_smoke.py -q
```

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
5. 插件模块导入期不产生 ``DeprecationWarning``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

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


if __name__ == "__main__":
    print(json.dumps(_probe(), ensure_ascii=False, indent=2))
    print("CONTRACT_OK")
