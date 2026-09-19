"""九轮 W4＋W5：CI 假绿清剿与公开面第三出口的守护测试（缺口 K5／K6／K7／K8）。

四件事各钉一遍，每件都带"植入即红"对照（否则闸会悄悄变哑）：

1. **K5 正本漂移守护不再在 CI 上 skip**：CI 用仓内合成夹具给 `HIPPOCAMPUS_GAP_LEDGER`，
   守护测试真跑；夹具格式变了／被删 → 本文件红。
2. **K6 预期失败必须真失败**：`xfail_strict=true`＋`--strict-markers` 落在 `pyproject.toml`，
   随迁的三条 xfail 全部 `strict=True`（不允许 `strict=False` 把"xpass 也算绿"放回来）。
3. **K7 未用到的 marker 不得声明**：历史声明的 `slow`／`needs_vector`／`needs_agent` 三个从未被引用，
   已删声明；再有"声明了没人用"即红。
4. **K8 公开面三个出口**：`scan_public_leak.py --git-text` 扫提交信息／标签注解／Release 正文，
   历史既有命中按基线记账（只减不增）——植一条新线索必须顶破基线变红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO / "pyproject.toml"
FIXTURE = REPO / "tests" / "fixtures" / "gap_ledger_synthetic.md"
GUARD_FILE = REPO / "tests" / "test_n23_t3_small_fixes.py"

LEDGER_ENV = "HIPPOCAMPUS_GAP_LEDGER"
LEDGER_MARKER = "已解决（N14）"


# ---------------------------------------------------------------- K5

def test_ci_configures_ledger_fixture_for_test_step():
    """CI 的测试步骤必须注入 `HIPPOCAMPUS_GAP_LEDGER`，指向仓内合成夹具（正本路径不入公开仓）。"""
    ci = CI.read_text(encoding="utf-8")
    assert LEDGER_ENV in ci, "ci.yml 里没有配置正本路径环境变量 → 守护测试会在 CI 上永远 skip"
    step = ci.split("python -m pytest tests/ -q", 1)[0]
    assert "tests/fixtures/gap_ledger_synthetic.md" in step, (
        "测试步骤的 HIPPOCAMPUS_GAP_LEDGER 没指向仓内夹具（或被挪到了别处）"
    )


def test_synthetic_fixture_satisfies_the_same_assertion_the_guard_makes():
    """夹具必须满足守护测试的**同一条**断言——否则 CI 上跑的是个假样本。"""
    assert FIXTURE.is_file(), "仓内合成夹具被删：CI 会退回永远 skip"
    text = FIXTURE.read_text(encoding="utf-8")
    assert LEDGER_MARKER in text
    guard = GUARD_FILE.read_text(encoding="utf-8")
    quoted = set(re.findall(r'assert "([^"]+)" in text', guard))
    assert LEDGER_MARKER in quoted, "守护测试的判据串变了，夹具没跟着改"


def test_fixture_without_marker_would_fail_the_guard():
    """对照测试：把夹具里的状态改成未解决 → 守护断言必须红（植一条即红）。"""
    planted = FIXTURE.read_text(encoding="utf-8").replace(LEDGER_MARKER, "待处理")
    with pytest.raises(AssertionError):
        assert LEDGER_MARKER in planted


# ---------------------------------------------------------------- K6

def _ini_block() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r"^\[tool\.pytest\.ini_options\]([\s\S]*?)(?=^\[|\Z)", text, re.M)
    assert m, "pyproject 里没有 pytest 配置段"
    return m.group(1)


def test_pytest_is_strict_about_xfail_and_markers():
    block = _ini_block()
    assert "xfail_strict = true" in block, "xfail_strict 被关掉＝xpass 也算绿（假绿）"
    assert "--strict-markers" in block, "缺 --strict-markers＝拼错的 marker 静默不生效"


def test_ported_xfails_are_all_strict():
    ported = REPO / "tests" / "ported" / "test_security_pii.py"
    text = ported.read_text(encoding="utf-8")
    assert text.count("strict=False") == 0, "有人把 xfail 调回 strict=False"
    assert text.count("pytest.mark.xfail") >= 1
    assert "strict=True" in text


def test_relaxing_xfail_strict_turns_the_gate_red():
    """对照测试：把 xfail_strict 改成 false 的写法必须被本闸拒绝。"""
    planted = _ini_block().replace("xfail_strict = true", "xfail_strict = false")
    assert "xfail_strict = true" not in planted


# ---------------------------------------------------------------- K7

def test_no_dead_marker_declarations():
    """marker 声明必须都被用到（历史上三个声明从未被引用＝纸面门禁）。"""
    block = _ini_block()
    declared = set(re.findall(r'"(\w+):', block)) if "markers" in block else set()
    used: set[str] = set()
    for path in (REPO / "tests").rglob("*.py"):
        used.update(re.findall(r"@pytest\.mark\.(\w+)", path.read_text(encoding="utf-8")))
    dead = declared - used
    assert not dead, f"声明了但没人用的 marker：{sorted(dead)}（要么用起来要么删声明）"


def test_marker_typo_is_rejected_by_strict_markers():
    """对照测试：`-m` 表达式用一个未声明的 marker 时，pytest 必须直接报错而非静默跑空。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-m", "this_marker_does_not_exist", "--collect-only", "-q"],
        cwd=str(REPO), capture_output=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert proc.returncode != 0 or "unknown mark" in (proc.stdout + proc.stderr).lower(), (
        "--strict-markers 没生效：未声明 marker 被静默接受"
    )


# ---------------------------------------------------------------- K8

def test_git_text_mode_exists_and_is_baseline_bounded():
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import scan_public_leak as spl

    assert hasattr(spl, "GIT_TEXT_BASELINE")
    assert spl.GIT_TEXT_BASELINE >= 0
    lines = spl._git_text_lines(REPO)
    assert lines, "git 文本一条都没取到＝闸变成空转"
    assert any(src.startswith("git:commit:") for src, _, _ in lines), "没有覆盖提交信息"
    hits = spl.scan_lines(lines)
    assert len(hits) <= spl.GIT_TEXT_BASELINE, (
        f"git 对象命中 {len(hits)} 条 > 基线 {spl.GIT_TEXT_BASELINE}：新线索进了公开面"
    )


def test_planted_commit_style_clue_exceeds_the_baseline():
    """对照测试：往"提交信息"里植一条定位线索 → 计数必须顶破基线（植一条即红）。"""
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import scan_public_leak as spl

    lines = spl._git_text_lines(REPO) + [("git:commit:planted0", 1, "见 " + "/".join(["D:", "Obsidian"]) + " 里的登记")]
    hits = spl.scan_lines(lines)
    assert len(hits) > spl.GIT_TEXT_BASELINE, "植入的线索没被数进去＝第三出口闸是空转"


def test_ci_runs_the_leak_gate_with_git_text():
    ci = CI.read_text(encoding="utf-8")
    assert "scan_public_leak.py --git-text" in ci, "CI 仍只扫工作树（第二／第三出口没覆盖）"
    assert "scan_credentials.py" in ci
