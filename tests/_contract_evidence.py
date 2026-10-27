"""契约冒烟的 evidence 解析与核对（纯函数，可单独单测）。

为什么要单独一个模块：这两件事都必须**在没有真实 AstrBot 源码树的机器上**也能被测到。
``test_contract_smoke.py`` 在缺 ``ASTRBOT_SRC`` 时会模块级 skip，把断言逻辑放在它里面就等于
让"守卫本身"永不被常规 ``pytest -q`` 覆盖 —— 而它正是用来防止"假绿"的那道闸。

背景（实测）：子进程做的是 ``sys.path.insert(0, ASTRBOT_SRC)`` 后 ``import astrbot``。
``ASTRBOT_SRC`` 指错目录时 Python 会**静默回落到 venv 里已安装的 AstrBot**，退出码仍是 0、
仍打印 ``CONTRACT_OK``。父测试若只断言"退出码 0 + 有 CONTRACT_OK"，就会得到一个
**证明不了任何事**的绿。所以这里用 ``astrbot.__file__`` 反查包路径，要求它确实落在
用户指定的那棵树之下。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class ContractEvidenceError(AssertionError):
    """evidence 缺失、无法解析，或与 ``ASTRBOT_SRC`` 不符。"""


def _normalised(path: str | os.PathLike[str]) -> str:
    """绝对化 + 规范化大小写，用于跨平台/跨盘符比较（Windows 大小写不敏感）。"""
    return os.path.normcase(str(Path(path).resolve()))


def is_strictly_under(
    path: str | os.PathLike[str], root: str | os.PathLike[str]
) -> bool:
    """``path`` 是否**位于** ``root`` 之下（不含相等；按分隔符边界，不做字符串前缀匹配）。

    刻意排除"相等"：``ASTRBOT_SRC`` 被写成 ``…/app/astrbot``（多一层）时，包路径与
    ``ASTRBOT_SRC`` **相等**，那种写法下 ``import astrbot`` 根本找不到包、只会回落到
    venv 里那份，必须判红。所以判据是"严格在下面"，不是"在里面或就是它"。
    """
    child, parent = _normalised(path), _normalised(root)
    return child.startswith(parent + os.sep)


def extract_evidence(output: str) -> dict:
    """从子进程输出里抠出最后一段"带 ``astrbot_package`` 的 JSON 对象"。

    子进程会先打一堆核心日志（可能含 ``{``），再打印 evidence JSON，最后一行
    ``CONTRACT_OK``。所以从后往前逐个 ``{`` 尝试 ``raw_decode``，取第一个能解成
    **含 ``astrbot_package`` 键的 dict** 的片段 —— 不依赖日志格式，也不依赖缩进。
    """
    decoder = json.JSONDecoder()
    for index in range(len(output) - 1, -1, -1):
        if output[index] != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(output, index)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "astrbot_package" in candidate:
            return candidate
    raise ContractEvidenceError(
        "子进程输出里找不到 evidence JSON（含 astrbot_package 的对象）。"
        "子进程可能没跑到 evidence 打印就退出了 —— 原始输出见下方：\n" + output[-4000:]
    )


def assert_evidence_matches(evidence: dict, astrbot_src: str) -> None:
    """断言"真的测了你指定的那棵树"，并顺带核对两条契约结论。

    1. ``astrbot.__file__`` 反查出的包目录必须位于 ``ASTRBOT_SRC`` 之下（关掉假绿）；
    2. ``provider_api`` 里必须有同步 ``get_using_provider``（4.23–4.26 唯一入口）；
    3. ``provider_gate`` 的 ``via`` 必须与 ``provider_api.async`` 一致，且两条判定结论
       仍是"``modalities=['text']`` 跳过 / ``modalities=None`` 放行"（现在只是打印，
       打印错了没人会发现）。
    """
    package = evidence.get("astrbot_package")
    if not isinstance(package, str) or not package.strip():
        raise ContractEvidenceError(
            f"evidence 里的 astrbot_package 不可用: {package!r}（应为字符串路径）"
        )
    if not is_strictly_under(package, astrbot_src):
        raise ContractEvidenceError(
            f"子进程实际导入的是 {package}，它**不在** ASTRBOT_SRC={astrbot_src} 之下。\n"
            "ASTRBOT_SRC 必须是**包含 astrbot/ 包的那一层目录**"
            "（例如 <AstrBot 仓库>/backend/app，而不是 …/backend/app/astrbot，"
            "也不是导出包的根）。\n"
            "这种绿是假的：子进程回落到 venv 里已安装的 AstrBot，"
            "本次冒烟没有测到你指定的那棵树。"
        )

    provider_api = evidence.get("provider_api")
    if not isinstance(provider_api, dict):
        raise ContractEvidenceError(f"evidence 缺少 provider_api: {provider_api!r}")
    if provider_api.get("sync") is not True:
        raise ContractEvidenceError(
            "真实 Context 上没有同步 get_using_provider —— 4.23–4.26 的唯一入口，"
            f"声明下界 4.23 不成立: {provider_api!r}"
        )

    gate = evidence.get("provider_gate")
    if not isinstance(gate, dict):
        raise ContractEvidenceError(f"evidence 缺少 provider_gate: {gate!r}")
    expected_via = "async" if provider_api.get("async") else "sync"
    if gate.get("via") != expected_via:
        raise ContractEvidenceError(
            f"provider_gate.via={gate.get('via')!r} 与 provider_api.async="
            f"{provider_api.get('async')!r} 不一致（应为 {expected_via!r}）"
        )
    if gate.get("modalities_text") != "skip":
        raise ContractEvidenceError(
            f"modalities=['text'] 应跳过注入，evidence 却是 {gate.get('modalities_text')!r}"
        )
    if gate.get("modalities_none") != "inject":
        raise ContractEvidenceError(
            f"modalities=None 应放行注入，evidence 却是 {gate.get('modalities_none')!r}"
        )
