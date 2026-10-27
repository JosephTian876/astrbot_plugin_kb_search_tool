"""R4 探针：插件配置在真实核心手里经历了什么？（决定 P2-1 两半各自的真实严重性）

结论（4.23.0 与 4.29.1 行为一致）：
- **拼错的键**：核心的 ``AstrBotConfig`` 会把它**删掉并写回文件**，同时打一条 INFO
  （4.29.1 ``Config key removed: <key>`` / 4.23.0 ``检查到配置项 <key> 不存在，将从当前配置中删除``）。
  所以正常路径（WebUI / 核心加载）下它到不了插件手里 —— 但那条 INFO 只说了"删了一个键"，
  没说明"你想改的开关没生效、正确键名是什么"，而且日志级别调到 WARNING 以上就看不见了。
- **类型错误（``"false"`` 字符串）**：核心**原样保留**，插件用 ``bool()`` 读成 ``True``
  → 用户的"关掉"被静默反转。这一半是本轮修复的主体，与核心无关。

只写 ``ASTRBOT_ROOT`` 下的临时目录，不碰生产 ``data/``。
用法：``PROBE_SRC=<astrbot 源码树> python kb-r4-probe-cfg.py``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(r"D:\UwU\Documents\dshworkdir\work\kb-030")

SRC = os.environ.get("PROBE_SRC", r"D:\AstrBot\backend\app")
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="kb_r4_cfg_")
sys.path.insert(0, SRC)

import astrbot  # noqa: E402
from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402

print("astrbot:", getattr(astrbot, "__version__", "<no __version__>"), "| src:", SRC)
print("root:", os.environ["ASTRBOT_ROOT"])

schema = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))

# 用户手改的配置文件：一个拼错的键 + 一个字符串 "false" + 一个合法键。
user_conf = {
    "enable": True,
    "respect_provider_tool_support": "false",
    "respect_provider_tools_support": False,
}
path = Path(os.environ["ASTRBOT_ROOT"]) / "plugin_config.json"
path.write_text(json.dumps(user_conf, ensure_ascii=False, indent=2), encoding="utf-8")

before = json.loads(path.read_text(encoding="utf-8"))
cfg = AstrBotConfig(config_path=str(path), schema=schema)
# 核心写回时用 utf-8-sig（带 BOM），读回来必须用 utf-8-sig。
after_file = json.loads(path.read_text(encoding="utf-8-sig"))

print("用户原文件      :", json.dumps(before, ensure_ascii=False, sort_keys=True))
print("插件拿到的 dict :", json.dumps(dict(cfg), ensure_ascii=False, sort_keys=True))
print("被改写后的文件  :", json.dumps(after_file, ensure_ascii=False, sort_keys=True))
print("拼错键还在吗    :", "respect_provider_tools_support" in cfg)
print('字符串 "false"  :', repr(cfg.get("respect_provider_tool_support")))
print("bool() 读到的   :", bool(cfg.get("respect_provider_tool_support")))
