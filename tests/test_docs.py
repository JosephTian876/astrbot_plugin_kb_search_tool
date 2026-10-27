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


def test_40_probe_and_contract_smoke_have_no_machine_specific_paths():
    """可复跑性：探针与契约冒烟里不能写死本机路径，``SRC`` 必须来自环境变量。

    为什么值得一条用例：这两份脚本是"结论的证据"。写死 ``D:\\UwU\\...`` 时它们在本机能跑、
    在别人机器上只会报"你的目录不存在"——证据就变成了不可复跑的断言（R5 §5.1 实测）。
    ``contract_smoke.py`` 的假绿守卫另见 ``tests/test_contract_evidence.py``。
    """
    markers = ("D:\\UwU", "dshworkdir", "D:\\AstrBot", "fix-venv", "C:\\Users")
    # README 也一起查：它的"契约冒烟"示例是用户照着抄的，写死别人的路径同样不可复跑
    # （而且那份示例里的 `$py` 曾经没有任何地方定义过）。
    for name in ("tests/probe_core_config.py", "tests/contract_smoke.py", "README.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"{name} 里还有本机路径: {marker}"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "$py" not in readme, "示例里的 $py 从来没有定义过，用户照抄会失败"

    probe = (REPO_ROOT / "tests" / "probe_core_config.py").read_text(encoding="utf-8")
    # 仓库根必须由 __file__ 反推（换克隆目录不用改代码）。
    assert "Path(__file__).resolve().parent.parent" in probe
    # SRC 必须必填：缺了就明确报错退出，而不是回落到某个本机默认值。
    assert 'os.environ.get("PROBE_SRC", "")' in probe
    assert "SystemExit(2)" in probe
    # 与契约冒烟同一条防假绿断言：实际导入的 astrbot 必须在 PROBE_SRC 之下。
    assert "astrbot.__file__" in probe


#: 审计/实现/架构过程文档的名字前缀。它们**不进仓库**（见 ``.gitignore``）：
#: 天然含真实 QQ 群号/好友号与生产 provider 清单，而本仓库会随插件市场被 clone 到每台
#: 用户机器上。过程流水账留在仓库外，面向用户的结论压缩进 README/CHANGELOG。
AUDIT_DOC_PREFIXES = ("AUDIT-", "IMPL-", "ARCH-", "VERIFY-")


def _text_files(root: Path) -> list[Path]:
    """工作区里的文本文件（跳过 vcs 目录、缓存目录与本用例自己）。"""
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".bench_root"}
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name == Path(__file__).name:
            continue  # 本文件自己必须写出这些前缀才能断言它们不存在
        files.append(path)
    return files


def test_41_audit_docs_are_not_in_the_repo_and_leave_no_dangling_references():
    """P1-3：审计文档不随仓库发布，且全仓不得留指向它们的悬空引用。

    三层断言，对应三种"复发"方式：
    1. 文件本身不在工作区 —— 只 ``gitignore`` 而不删除，市场打包时仍可能带上；
    2. ``.gitignore`` 里有规则 —— 否则下次有人新建 ``AUDIT-X.md`` 又会被 ``git add .`` 带进来；
    3. 源码/测试/文档里不再出现这四个前缀 —— 否则读者会去找一份仓库里根本没有的文件，
       更糟的是某个测试读它，仓库在别人机器上就跑不起来。
    """
    files = _text_files(REPO_ROOT)
    assert files, "工作区扫描为空，扫描逻辑有问题"

    # 1. 审计文档不在仓库里（按前缀找，不写死具体文件名）。
    present = [
        path.name
        for path in files
        if path.suffix == ".md" and path.name.startswith(AUDIT_DOC_PREFIXES)
    ]
    assert present == [], f"审计文档仍在仓库工作区里: {present}"

    # 2. .gitignore 覆盖这四个前缀（否则下次照样会被误提交）。
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for prefix in AUDIT_DOC_PREFIXES:
        assert f"{prefix}*.md" in gitignore, f".gitignore 缺 {prefix}*.md 规则"

    # 3. 没有任何文件还引用它们。
    offenders = []
    for path in files:
        if path.suffix not in {".py", ".md", ".json", ".yaml", ".toml", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for prefix in AUDIT_DOC_PREFIXES:
                if prefix in line:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}"
                    )
    assert offenders == [], "还有指向仓库外审计文档的悬空引用:\n" + "\n".join(offenders)


def test_42_tests_never_read_the_audit_docs():
    """任何测试都不得依赖那两份审计文档存在（否则仓库在别人机器上就跑不起来）。

    与 ``test_41`` 的区别：41 查"文本里还提不提"，这条查"**真的有人去读它**"——
    ``Path(...).read_text()`` / ``open(...)`` 之类会被 41 漏掉的用法（例如拼接文件名）。
    """
    for path in _text_files(REPO_ROOT / "tests"):
        if path.suffix != ".py":
            continue
        # 本文件（test_docs.py）由 _text_files 跳过：它必须写出这些前缀才能断言它们不存在。
        text = path.read_text(encoding="utf-8")
        for prefix in AUDIT_DOC_PREFIXES:
            assert f'"{prefix}' not in text and f"'{prefix}" not in text, (
                f"{path.name} 里用字符串常量引用了审计文档 {prefix}*.md"
            )


def test_60_readme_has_a_symptom_first_troubleshooting_table():
    """P2-2：README 要有一张「症状 → 先看哪里 → 最可能的原因」表。

    为什么值得一条用例：面板 hint 已压到摘要，**唯一能读长文的地方就是 README**；
    而用户是带着"工具没出现"来的，不是带着"我想读日志"来的。没有症状入口时，
    最常见的结局是"插件没生效，但我不知道要去哪里看"。
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "| 症状 |" in readme, "缺少症状表的表头"
    assert "| 第一步看哪里 |" in readme
    assert "| 最可能的原因 |" in readme
    for symptom in (
        "对话里模型完全不用工具",
        "一次注入日志都没有",
        "没有可用知识库",
        "未勾选 tool_use",
        "插件日志一行都没有",
    ):
        assert symptom in readme, f"症状表里缺这一行: {symptom}"

    # 表格必须真的是一张 markdown 表：表头行 + 分隔行 + 至少 6 行数据。
    lines = readme.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("| 症状 |"))
    assert lines[header + 1].startswith("|---"), "症状表缺少 markdown 分隔行"
    rows = 0
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        rows += 1
    assert rows >= 6, f"症状表只有 {rows} 行，至少要覆盖 6 种症状"


def test_61_readme_carries_the_full_judgement_that_left_the_panel_hint():
    """P2-2：从 hint 里搬走的长文必须真的落在 README，而不是被删掉了。

    面板 hint 的定位是"悬停时能读完的摘要"。摘要里保留的是**判据关键词**，细节（fail-open
    的每一种形态、图片 fallback、只读承诺）只能在 README 里。这条用例保证"压缩"不是
    "丢内容"。
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "### 「尊重模型能力」的完整判据" in readme, "README 里没有完整判据小节"
    for detail in (
        "tool_use",
        "非空 list",
        "fail-open",
        "不是 list",
        "取不到 provider",
        "抛异常",
        "fallback provider",
        "只读",
    ):
        assert detail in readme, f"完整判据里缺这一条: {detail}"

    # 反向钉子：判据细节不该再塞回面板 hint（那会立刻超过 test_28 的长度上界）。
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    respect_hint = schema["respect_provider_tool_support"]["hint"]
    assert "fallback" not in respect_hint
    assert "4.23" not in respect_hint


def test_62_equivalence_promise_is_narrowed_and_names_its_exceptions():
    """P2-3：与 0.2.0 的等价承诺必须**收窄**，且把已声明的差异写清楚。

    旧口径是"142/1260 个场景观测 JSON SHA256 完全相同"—— 那套 harness 在仓库外、谁也无法
    复跑，而它恰好唯一挡住了"非标准 `func_tool` 每请求刷 ERROR"这条路径。新口径只承诺
    **能在这里复验的三件事**：不查询 provider、注入决策一致、日志行为不变（除已声明的
    两处）。这条用例钉住措辞与那两处差异都在，避免哪天又漂回不可复跑的表述。
    """
    for name in ("CHANGELOG.md", "README.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        # 承诺的三个可复验部分。
        assert "注入决策与 0.2.0 完全一致" in text, f"{name} 缺'注入决策'承诺"
        assert "日志行为不相对于 0.2.0" in text, f"{name} 缺'日志行为'承诺"
        assert "不查询 provider（0 次）" in text, f"{name} 缺「不查询 provider」承诺"
        # 两处已声明的差异都必须写明（否则承诺就是空头支票）。
        assert "非标准 `func_tool`" in text, f"{name} 没写第 1 处已声明差异"
        assert "判定对象" in text, f"{name} 没写第 2 处已声明差异"
        # 旧口径必须消失：它既不可复跑，又被本版的行为变化推翻。
        # （允许在"为什么改口径"那一段里**引用**旧措辞，但那时段落里必须写明是旧版本写的。）
        assert "逐字节回到 0.2.0" not in text, f"{name} 还留着旧的'逐字节'口径"
        for paragraph in text.split("\n\n"):
            if "SHA256" in paragraph:
                assert "旧版" in paragraph, (
                    f"{name} 里仍把 SHA256 当作承诺在陈述，而不是在解释旧措辞:\n{paragraph}"
                )

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "一次性 WARNING" in changelog, "行为变化本身要写进 CHANGELOG"
    assert "每请求一条带 traceback 的 ERROR" in changelog, "要写清旧行为是什么"


def test_63_promise_is_backed_by_a_runnable_test_not_by_the_offline_matrix():
    """承诺必须由**仓库内**的用例钉住：``test_29`` 断言 provider 0 次 + 日志逐条相同。

    这条是结构钉：如果哪天有人删掉 ``test_29``（或把它改成只断言"注入了"），承诺就又变成
    口头保证。这里同时钉住它覆盖了 modalities 的各种形态与有无知识库两个维度。
    """
    source = (REPO_ROOT / "tests" / "test_main.py").read_text(encoding="utf-8")

    name = "def test_29_switch_off_is_observably_identical_to_0_2_0"
    assert name in source
    start = source.index(name)
    # 装饰器在 def 之前：往前多取一段，后面截到下一个 def。
    block = source[max(0, start - 600) : start]
    block += source[start : source.index("\ndef ", start)]
    assert "@pytest.mark.parametrize" in block, (
        "test_29 必须是参数化矩阵，不能只测一种形态"
    )
    assert '"modalities"' in block and '"kb_names"' in block, (
        "test_29 要覆盖 modalities 的各种形态与有无知识库两个维度"
    )
    assert 'config={"respect_provider_tool_support": False}' in block
    assert "provider_calls == []" in block, "必须断言 provider 一次都没查"
    assert "messages(logs, logging.DEBUG) ==" in block, "必须逐字断言日志"
    assert "_ProviderMustNotBeTouched" in source, (
        "开关关闭时要用 BaseException 陷阱证明'一次都没碰 provider'"
    )
