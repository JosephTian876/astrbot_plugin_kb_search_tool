"""pytest 公共准备：装 ``astrbot`` 替身、加载插件模块、为每个用例准备干净状态。

约束（与 README「开发」一致）：**脱网**、**不启动 AstrBot**、**不写任何真实数据目录**。
替身里根本没有 ``astrbot.core``（见 ``_astrbot_stub`` 的模块注释），所以任何一次
``import astrbot`` 都会当场失败；``ASTRBOT_ROOT`` 仍指向临时目录，作为第二道保险。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

if str(TESTS_DIR) not in sys.path:  # 让 test_*.py 能直接 `import _astrbot_stub`
    sys.path.insert(0, str(TESTS_DIR))

import _astrbot_stub  # noqa: E402 - 必须先于任何插件导入完成替身注册
from _astrbot_stub import CAPTURED, FILTER, SP  # noqa: E402

#: 被测模块名。插件只有一个文件 ``main.py``（AstrBot 硬要求该文件名），按路径加载可避免与
#: 任何已安装的同名顶层模块冲突。
PLUGIN_MODULE_NAME = "kbtool_plugin_under_test"


def _load_plugin():
    """把 ``main.py`` 作为模块加载一次；模块级代码（含钩子注册）因此只执行一次。"""
    path = REPO_ROOT / "main.py"
    spec = importlib.util.spec_from_file_location(PLUGIN_MODULE_NAME, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


#: 让插件模块与测试模块不落 ``.pyc``。注意：``conftest``/``_astrbot_stub`` 这两份由解释器在
#: 导入期写（本行生效之前），已在 ``.gitignore`` 里忽略 ``__pycache__/``。
sys.dont_write_bytecode = True
_astrbot_stub.install()
PLUGIN = _load_plugin()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """每个用例都从空日志、空会话配置、临时 ``ASTRBOT_ROOT`` 开始。"""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "astrbot_root"))
    CAPTURED.clear()
    SP.session.clear()
    SP.error = None
    yield
    CAPTURED.clear()
    SP.session.clear()
    SP.error = None


@pytest.fixture
def plugin_module():
    """被测插件模块（``main.py``）。"""
    return PLUGIN


@pytest.fixture
def hook_kwargs():
    """模块导入时钩子注册收到的完整 kwargs 列表（注册只发生一次）。"""
    return FILTER.on_llm_request_kwargs


@pytest.fixture
def logs():
    """本次用例捕获到的日志记录（替身 logger 写入）。"""
    return CAPTURED


@pytest.fixture
def sp():
    """会话级偏好设置替身（``astrbot.api.sp``）。"""
    return SP


@pytest.fixture
def hook():
    """同步跑一次真实钩子入口 ``on_llm_request``（钩子本身是 async）。

    刻意不用 ``pytest-asyncio``：测试套件不该为一次 ``asyncio.run`` 引入额外插件依赖。
    """

    def run(plugin, req, umo: str = "aiocqhttp:FriendMessage:10086"):
        event = _astrbot_stub.AstrMessageEvent(umo)
        asyncio.run(plugin.on_llm_request(event, req))
        return event

    return run
