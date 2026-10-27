"""文档与元数据的一致性测试（不需要导入插件，纯文本核对）。

与 ``test_main.py`` 的分工：那边测**行为**（走真实钩子入口），这里测**声明**
（版本号、CHANGELOG、README 三处是否说同一件事）。分开的理由很实际——这两类断言
失败时的修法完全不同：行为错改代码，声明错改文档。

为什么版本号值得一条用例：版本号的单一事实源是 ``metadata.yaml``，但用户读到的是
README（「X.Y.Z 起……」）与 CHANGELOG（节标题）。三处任何一处漏改，市场页的版本、
升级提示与实际行为就会对不上，而这类错误在代码评审里最容易看漏（它不会让任何
行为测试变红）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 本轮定稿的版本（Lead 决定）。改版本号时**必须**同步这里、``metadata.yaml``、
#: ``CHANGELOG.md`` 顶部节标题与 ``README.md`` 的版本提及 —— 用例会逐个核对。
FINAL_VERSION = "0.3.0"

#: 未发布的中间版本号。只允许作为「未发布，本次合并为 0.3.0」的说明在 CHANGELOG
#: 顶部节里出现**一次**；出现在别处就是残留（用户会以为线上跑的是那个版本）。
#: 这是全仓唯一必须写出这个字面量的地方 —— 它正是用来**断言它不存在**的针。
UNRELEASED_VERSION = "0.2.1"


def test_31_version_is_finalized_as_0_3_0():
    """版本定稿 ``0.3.0``：metadata / CHANGELOG 顶部 / README 一致，且旧号无残留。

    定 ``0.3.0`` 的理由（SemVer minor）：新增配置键 ``respect_provider_tool_support``
    **且**默认行为变化（部分 provider 不再注入工具）—— 不是纯 bugfix，不能走 patch 号。
    """
    metadata = yaml.safe_load((REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["version"] == FINAL_VERSION

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [line for line in changelog.splitlines() if line.startswith("## ")]
    assert headings, "CHANGELOG 没有任何版本节"
    top = headings[0]
    assert top.startswith(f"## {FINAL_VERSION}"), (
        f"CHANGELOG 顶部节不是定稿版本: {top!r}"
    )
    # 「未发布」那句说明必须留着（否则读者不知道那个号为什么消失了），
    # 而且**只能出现一次**：其余位置出现就是残留。
    assert changelog.count(UNRELEASED_VERSION) == 1, (
        f"CHANGELOG 里旧号应只在顶部节作为合并说明出现一次，"
        f"实际 {changelog.count(UNRELEASED_VERSION)} 次"
    )
    assert "未发布" in changelog and "合并" in changelog

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert UNRELEASED_VERSION not in readme, "README 里还有旧版本号残留"
    assert FINAL_VERSION in readme, "README 至少要在「版本兼容」里提到定稿版本"

    # 代码/配置/元数据里绝不能出现旧版本号：这些文件描述的是"当前状态"，不是历史记录。
    for name in ("main.py", "_conf_schema.json", "metadata.yaml", "pyproject.toml"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert UNRELEASED_VERSION not in text, f"{name} 里残留旧版本号"


def test_38_conf_schema_covers_every_config_key_the_plugin_reads():
    """配置键 1:1：``_conf_schema.json`` 的键 == ``main.py`` 的 ``_KNOWN_CONFIG_KEYS``。

    ``_KNOWN_CONFIG_KEYS`` 是"未知键告警"的白名单。它一旦与 schema 漂移，要么把合法键
    当成拼写错误告警（噪音），要么让面板上的开关被静默忽略 —— 两种都很难在行为测试里
    暴露：行为测试直接构造 config 字典，根本不经过 schema。
    """
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    plugin_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")

    match = re.search(
        r"_KNOWN_CONFIG_KEYS = frozenset\(\s*\{(.*?)\}\s*\)", plugin_source, re.S
    )
    assert match is not None, "main.py 里找不到 _KNOWN_CONFIG_KEYS 的字面量"
    declared = set(re.findall(r'"([^"]+)"', match.group(1)))

    assert declared == set(schema), (
        f"_KNOWN_CONFIG_KEYS {sorted(declared)} 与 schema {sorted(schema)} 不一致"
    )
    for key, item in schema.items():
        assert item["type"] == "bool", f"{key} 必须是 bool（面板要渲染成开关）"
        assert item["default"] is True, f"{key} 的默认值应为 true"


def test_39_string_false_risk_is_backed_by_a_runnable_probe():
    """README/CHANGELOG 对"字符串 `"false"`"的结论必须有可复跑的探针做依据。

    这条钉的是**文档与证据的对应关系**：只要文档里保留了"核心不纠正类型、插件必须显式解析"
    这个结论，仓库里就必须有那个探针脚本，且探针真的测了这两件事 —— 否则它只是一份
    "看起来像证据"的脚本，读者无法复跑。
    """
    probe = REPO_ROOT / "tests" / "probe_core_config.py"
    source = probe.read_text(encoding="utf-8")

    # 探针必须真的测这两件事。
    assert "respect_provider_tools_support" in source, "探针没测拼错的键"
    assert "bool(" in source, "探针没测 bool() 的真实语义"
    assert "AstrBotConfig" in source, "探针必须走真实核心的配置类"

    # 两份面向用户的文档都要指得到它，否则结论就成了不可复跑的断言。
    for name in ("README.md", "CHANGELOG.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "probe_core_config.py" in text, (
            f"{name} 讲了字符串 false 的风险，却没指向 tests/probe_core_config.py"
        )
