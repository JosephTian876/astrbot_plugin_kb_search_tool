"""``tests/_contract_evidence.py`` 的单测：**假绿守卫本身**必须被常规 ``pytest -q`` 覆盖。

为什么单独一个文件：``contract_smoke.py`` 缺 ``ASTRBOT_SRC`` 时会模块级 skip，把守卫逻辑
写在它里面就等于让守卫永不被常规测试跑到 —— 而它正是用来防止"绿了但什么都没测到"的那道闸。
这里的用例全部脱网、不导入 ``astrbot``，只喂字符串与路径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from _contract_evidence import (
    ContractEvidenceError,
    assert_evidence_matches,
    extract_evidence,
    is_strictly_under,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _evidence(package, *, api=None, gate=None) -> dict:
    """造一份形状与子进程输出一致的 evidence。"""
    return {
        "astrbot_package": str(package),
        "ToolSet": "astrbot.core.agent.tool.ToolSet",
        "provider_api": api if api is not None else {"async": True, "sync": True},
        "provider_gate": (
            gate
            if gate is not None
            else {
                "modalities_text": "skip",
                "modalities_none": "inject",
                "via": "async",
            }
        ),
    }


def _output(evidence: dict) -> str:
    """复刻子进程的真实输出形状：核心日志 + evidence JSON + CONTRACT_OK。"""
    log = "[10:00:00.000] [Core] [INFO] [star.base:44]: Plugin X log level: INFO.\n"
    return log + json.dumps(evidence, ensure_ascii=False, indent=2) + "\nCONTRACT_OK\n"


# ---------------------------------------------------------------------------
# 路径包含关系：必须按分隔符边界判断，不能是字符串前缀
# ---------------------------------------------------------------------------


def test_is_strictly_under_accepts_descendant(tmp_path):
    root = tmp_path / "backend" / "app"
    (root / "astrbot").mkdir(parents=True)
    assert is_strictly_under(root / "astrbot", root), "包目录就在树里"


def test_is_strictly_under_rejects_equality(tmp_path):
    """``ASTRBOT_SRC=…/app/astrbot``（多一层）时两者相等 —— 那种写法是错的，必须判否。

    那种写法下 ``import astrbot`` 在 ``…/app/astrbot`` 里找不到 ``astrbot/`` 子目录，
    只会静默回落到 venv 里那份，测到的不是用户以为的那棵树。
    """
    root = tmp_path / "backend" / "app"
    (root / "astrbot").mkdir(parents=True)
    assert not is_strictly_under(root / "astrbot", root / "astrbot")


def test_is_strictly_under_rejects_prefix_lookalike(tmp_path):
    """``…/app2`` 不是 ``…/app`` 的子目录 —— 字符串前缀匹配会在这里误判。"""
    root = tmp_path / "app"
    lookalike = tmp_path / "app2" / "astrbot"
    lookalike.mkdir(parents=True)
    assert not is_strictly_under(lookalike, root)


def test_is_strictly_under_rejects_sibling_tree(tmp_path):
    """真实事故的复刻：venv 里那份 AstrBot 与本机源码树是两棵无关的树。"""
    src = tmp_path / "src" / "backend" / "app"
    venv = tmp_path / "venv" / "site-packages"
    (src / "astrbot").mkdir(parents=True)
    (venv / "astrbot").mkdir(parents=True)
    assert not is_strictly_under(venv / "astrbot", src)


def test_is_strictly_under_is_case_insensitive_on_windows(tmp_path):
    """Windows 上 ``d:\\astrbot`` 与 ``D:\\AstrBot`` 是同一个路径，不能判成"不在树下"。"""
    root = tmp_path / "AstrBot" / "backend"
    (root / "astrbot").mkdir(parents=True)
    assert is_strictly_under(str(root / "astrbot").upper(), root)


# ---------------------------------------------------------------------------
# evidence 解析：从"日志 + JSON + 标记行"里抠出对象
# ---------------------------------------------------------------------------


def test_extract_evidence_ignores_log_noise_and_trailing_marker():
    evidence = _evidence(r"D:\src\astrbot")
    assert extract_evidence(_output(evidence)) == evidence


def test_extract_evidence_survives_braces_in_logs(tmp_path):
    """核心日志里出现 ``{`` 时不能把解析带偏（真实日志确实会带 JSON 片段）。"""
    evidence = _evidence(tmp_path / "astrbot")
    noisy = 'some log {"a": 1} and another {broken\n' + _output(evidence)
    assert extract_evidence(noisy) == evidence


def test_extract_evidence_raises_readable_error_when_missing():
    with pytest.raises(ContractEvidenceError) as excinfo:
        extract_evidence("just logs\nCONTRACT_OK\n")
    assert "evidence JSON" in str(excinfo.value)
    assert "CONTRACT_OK" in str(excinfo.value), "报错要带上原始输出，便于定位"


# ---------------------------------------------------------------------------
# 核心断言：真的测了你指定的那棵树吗
# ---------------------------------------------------------------------------


def test_assert_evidence_accepts_the_tree_that_was_asked_for(tmp_path):
    src = tmp_path / "backend" / "app"
    (src / "astrbot").mkdir(parents=True)
    assert_evidence_matches(_evidence(src / "astrbot"), str(src))


def test_assert_evidence_rejects_fallback_to_another_astrbot(tmp_path):
    """**这条就是"假绿"的钉子**：ASTRBOT_SRC 指错目录 → 子进程回落到 venv 里那份。

    旧行为下父测试只看"退出码 0 + 有 CONTRACT_OK"，于是报 1 passed（已实测）。
    """
    src = tmp_path / "empty-tree"
    src.mkdir()
    venv_pkg = tmp_path / "venv" / "site-packages" / "astrbot"
    venv_pkg.mkdir(parents=True)

    with pytest.raises(ContractEvidenceError) as excinfo:
        assert_evidence_matches(_evidence(venv_pkg), str(src))
    message = str(excinfo.value)
    assert "不在" in message and str(venv_pkg) in message, "必须点明实际导入的是哪棵树"
    assert "假的" in message, "要明确告诉用户这种绿是假的"


def test_assert_evidence_rejects_too_deep_astrbot_src(tmp_path):
    """把 ``ASTRBOT_SRC`` 写成 ``…/app/astrbot``（多一层）也是常见错法。"""
    src = tmp_path / "backend" / "app"
    (src / "astrbot").mkdir(parents=True)
    with pytest.raises(ContractEvidenceError):
        assert_evidence_matches(_evidence(src / "astrbot"), str(src / "astrbot"))


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_assert_evidence_rejects_unusable_package_field(bad):
    evidence = _evidence("x")
    evidence["astrbot_package"] = bad
    with pytest.raises(ContractEvidenceError):
        assert_evidence_matches(evidence, str(REPO_ROOT))


def test_assert_evidence_requires_sync_provider_api(tmp_path):
    """同步 ``get_using_provider`` 是 4.23–4.26 的唯一入口：缺了就是下界不成立。"""
    src = tmp_path / "app"
    (src / "astrbot").mkdir(parents=True)
    evidence = _evidence(src / "astrbot", api={"async": True, "sync": False})
    with pytest.raises(ContractEvidenceError) as excinfo:
        assert_evidence_matches(evidence, str(src))
    assert "4.23" in str(excinfo.value)


@pytest.mark.parametrize(
    ("api", "gate"),
    [
        # via 与 provider_api 不一致（旧版回退路径记错了）
        (
            {"async": False, "sync": True},
            {"modalities_text": "skip", "modalities_none": "inject", "via": "async"},
        ),
        # 判定结论被写反：modalities=['text'] 其实注入了
        (
            {"async": True, "sync": True},
            {"modalities_text": "inject", "modalities_none": "inject", "via": "async"},
        ),
        # 判定结论被写反：modalities=None 其实被跳过了
        (
            {"async": True, "sync": True},
            {"modalities_text": "skip", "modalities_none": "skip", "via": "async"},
        ),
    ],
)
def test_assert_evidence_checks_provider_gate_conclusions(tmp_path, api, gate):
    """这两条以前只打印、没人核对：打印错了照样绿。"""
    src = tmp_path / "app"
    (src / "astrbot").mkdir(parents=True)
    evidence = _evidence(src / "astrbot", api=api, gate=gate)
    with pytest.raises(ContractEvidenceError):
        assert_evidence_matches(evidence, str(src))


def test_assert_evidence_accepts_sync_fallback_shape(tmp_path):
    """4.23–4.26 的形状：没有 async 方法，``via`` 必须是 ``sync``，同样要通过。"""
    src = tmp_path / "app"
    (src / "astrbot").mkdir(parents=True)
    evidence = _evidence(
        src / "astrbot",
        api={"async": False, "sync": True},
        gate={"modalities_text": "skip", "modalities_none": "inject", "via": "sync"},
    )
    assert_evidence_matches(evidence, str(src))


def test_contract_smoke_still_uses_the_guard():
    """结构钉：``contract_smoke.py`` 真的调用了守卫（否则上面的单测只测了一个孤儿模块）。"""
    source = (REPO_ROOT / "tests" / "contract_smoke.py").read_text(encoding="utf-8")
    assert "assert_evidence_matches" in source
    assert "extract_evidence" in source
    # 守卫必须在 `CONTRACT_OK` 断言**之后**仍然执行：不能只在失败分支里跑。
    ok_index = source.index('assert "CONTRACT_OK" in output')
    guard_index = source.index("assert_evidence_matches(evidence, ASTRBOT_SRC)")
    assert ok_index < guard_index


def test_contract_smoke_docstring_has_no_machine_specific_paths():
    """P1-1 的另一半：文件头不能再写死本机绝对路径（换成占位符）。"""
    source = (REPO_ROOT / "tests" / "contract_smoke.py").read_text(encoding="utf-8")
    for marker in ("D:\\UwU", "dshworkdir", "D:\\AstrBot", "fix-venv"):
        assert marker not in source, f"contract_smoke.py 里还有本机路径: {marker}"


def test_contract_evidence_module_is_importable_without_real_astrbot():
    """守卫不能把**真实** ``astrbot`` 拖进进程：常规 ``pytest -q`` 里它必须能独立导入。

    ``conftest`` 已把 ``astrbot`` 换成替身（``_astrbot_stub``），所以这里断言"拿到的是替身"，
    而不是"模块不存在"—— 后者在测试进程里永远不成立。
    """
    package = sys.modules.get("astrbot")
    assert package is not None, "conftest 应已装好替身"
    assert getattr(package, "__kbtool_stub__", False), (
        "守卫不该把真实 astrbot 拉进测试进程"
    )
