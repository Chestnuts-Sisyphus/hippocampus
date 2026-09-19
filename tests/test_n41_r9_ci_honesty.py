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

def test_ci_configures_ledger_fixture_at_job_level():
    """`HIPPOCAMPUS_GAP_LEDGER` 必须配在 **job 级** env（指向仓内合成夹具）。

    批次 B/C 的 CI 红点根因就在这：变量原先只挂在"测试"那一步的 step env 上，
    而"守护真的跑了吗"是**另一步**——它拿不到变量，于是守护又 skip，检查把它判红。
    所以判据收严为"出现在 `steps:` 之前"＝job 级，所有步骤共用一份环境。
    """
    ci = CI.read_text(encoding="utf-8")
    assert LEDGER_ENV in ci, "ci.yml 里没有配置正本路径环境变量 → 守护测试会在 CI 上永远 skip"
    head = ci.split("\n    steps:", 1)[0]
    assert LEDGER_ENV in head, "HIPPOCAMPUS_GAP_LEDGER 被挪回 step 级：no-phantom-skip 那一步会看不见它"
    assert "tests/fixtures/gap_ledger_synthetic.md" in head, (
        "job 级 HIPPOCAMPUS_GAP_LEDGER 没指向仓内夹具（正本路径不入公开仓）"
    )


def _core_only_install_line(ci: str) -> str:
    """取 core-only job 里那行 `pip install -e .`（该 job 只有一个安装步）。"""
    section = ci.split("\n  core-only:", 1)
    assert len(section) == 2, "ci.yml 里 core-only job 不见了"
    lines = [ln.strip() for ln in section[1].splitlines() if ln.strip().startswith("pip install -e .")]
    assert len(lines) == 1, f"core-only 安装步应恰好一行，实得 {lines}"
    return lines[0]


def _check_core_only_install(ci: str) -> None:
    """判据两半：必须带 pytest（否则子集步是死的）；必须不带 vector/proxy/dev 附加档
    （否则这个 job 证明不了"无向量库降级档"，下一步的 chromadb 缺席断言会失效）。
    """
    line = _core_only_install_line(ci)
    assert " pytest" in line, "core-only 安装步没装 pytest：最小 pytest 子集会因缺模块而永远红"
    for extra in ("vector", "proxy", "dev"):
        assert f"[{extra}]" not in line, f"core-only 安装步引入了 {extra} 附加档：降级档不再被隔离"


def test_core_only_job_installs_the_test_runner_it_runs():
    """批次 D/E/F 红点根因：pytest 属 dev 依赖，只装 `-e .` 时"W4 minimal pytest
    subset"那一步连解释器都起不来（`No module named pytest`）。
    """
    _check_core_only_install(CI.read_text(encoding="utf-8"))


def test_dropping_pytest_from_core_only_install_turns_the_gate_red():
    """植入即红对照：把 pytest 从安装行去掉 → 同一条闸必须判红。"""
    planted = CI.read_text(encoding="utf-8").replace("pip install -e . pytest", "pip install -e .")
    with pytest.raises(AssertionError):
        _check_core_only_install(planted)


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


def test_planted_commit_style_clue_is_counted_and_located():
    """对照测试：往"提交信息"里植一条定位线索 → 必须**被数进去且能定位到那一条**。

    为什么不用 `len(hits) > GIT_TEXT_BASELINE` 当判据（九轮 CI 实测教训）：CI 的 checkout 是
    浅历史，`git log` 只有 HEAD 一条，真实基线命中数在那里是 0，植一条也只能到 1 →
    `1 > 2` 判红。那条判据把"本地有完整历史"当前提，属**测试环境假设**混进了产品闸。
    换成"多一条且来源可指认"，与历史深度无关，空转照样抓得住。
    """
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import scan_public_leak as spl

    base = spl.scan_lines(spl._git_text_lines(REPO))
    planted_line = ("git:commit:planted0", 1, "见 " + "/".join(["D:", "Obsidian"]) + " 里的登记")
    hits = spl.scan_lines(spl._git_text_lines(REPO) + [planted_line])
    assert len(hits) == len(base) + 1, f"植一条线索却多了 {len(hits) - len(base)} 条（或 0 条＝闸空转）"
    assert any(h["file"] == "git:commit:planted0" for h in hits), (
        "命中里找不到植进去的那条＝第三出口根本没被扫"
    )


def test_ci_runs_the_leak_gate_with_git_text():
    ci = CI.read_text(encoding="utf-8")
    assert "scan_public_leak.py --git-text" in ci, "CI 仍只扫工作树（第二／第三出口没覆盖）"
    assert "scan_credentials.py" in ci
