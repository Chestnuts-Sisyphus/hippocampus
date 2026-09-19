"""八轮 V3 守护：公开面泄露闸 `scripts/scan_public_leak.py` 要有对照测试。

闸没有对照 = 不知道它到底拦不拦（七轮 T9 的教训）。三侧都要成立：
- **拦得住**：任务书列的每种泄露形态各植入一条，必须命中；**含 `*.py` 探针**——
  本轮第一批脱敏漏扫 `tests/` 正是因为闸只管文档，这条形态单独钉一次；
- **删得干净**：同一目录删掉植入文件后必须零命中（证明命中来自植入内容，不是闸在乱叫）；
- **不自盲**：扫描器自身与本测试文件都在扫描范围内、零豁免——
  待查串一律片段拼接，所以源码里不出现完整泄露串，若哪天有人写成字面量，本闸当场红。

（本文件里的假路径一律用拼接写出，避免闸命中自己的测试样例。）
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "scan_public_leak.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("scan_public_leak", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _probe(scanner, name: str, suffix: str, text: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}{suffix}"
        path.write_text(text, encoding="utf-8")
        return scanner.scan_files([path])


# 泄露形态 → 植入样例（片段拼接，源码里不出现完整串）
PLANTED = {
    "凭据母库目录": ("D:" + "/AI" + "/" + "KEY" + "/ALL.txt"),
    "凭据登记簿文件名": ("GITHUB" + "-" + "TOKENS" + ".txt"),
    "行号指针": ("登记簿第 " + "11 " + "行是可用凭据"),
    "知识库正本目录": ("D:" + "/Obsidian" + "/_wiki"),
    "当前事实源目录": ("求职" + "-" + "天津" + "秋招-202609"),
    "本机用户目录": ("C:" + "/Users" + "/" + "someuser" + "/Documents"),
    "POSIX 主目录": ("/home/" + "someuser" + "/proj"),
    "本机账号名": ("Admini" + "strator" + " 的会话目录"),
}


def test_every_leak_shape_is_caught():
    """召回侧：每一种泄露形态植入都必须命中。"""
    scanner = _scanner()
    for name, line in PLANTED.items():
        findings = _probe(scanner, "probe", ".md", f"说明文字 {line} 尾巴\n")
        assert findings, f"泄露形态漏检：{name}"


def test_source_files_are_in_scope_not_only_docs():
    """回归本轮真实踩过的坑：第一批脱敏只扫文档，漏了 `tests/` 里硬编码的本机个人目录路径。

    所以 `*.py` 探针必须与 `*.md` 同等被拦。
    """
    scanner = _scanner()
    line = "LEDGER = " + repr(PLANTED["凭据母库目录"])
    py_findings = _probe(scanner, "probe_in_tests", ".py", f"# 顶部注释\n{line}\nVALUE = 1\n")
    md_findings = _probe(scanner, "probe_in_docs", ".md", f"{line}\n")
    assert py_findings, "扫描范围漏了 *.py（会重演只扫文档的错）"
    assert md_findings, "扫描范围漏了 *.md"


def test_planted_then_removed_is_clean():
    """对照侧：植入→命中，删除→零命中（命中不是闸自己造出来的）。"""
    scanner = _scanner()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "notes.md"
        target.write_text("干净的说明，没有任何定位线索。\n", encoding="utf-8")
        assert scanner.scan_files([target]) == [], "基线就有命中，对照不成立"
        target.write_text("干净一行\n" + PLANTED["知识库正本目录"] + "\n", encoding="utf-8")
        assert scanner.scan_files([target]), "植入后应命中"
        target.write_text("干净的说明，没有任何定位线索。\n", encoding="utf-8")
        assert scanner.scan_files([target]) == [], "删掉植入后仍命中，说明在乱叫"


def test_scanner_and_its_test_are_not_exempt():
    """不自盲：扫描器自身与本测试文件都过一遍规则，必须零命中（无豁免名单）。"""
    scanner = _scanner()
    findings = scanner.scan_files([SCRIPT, Path(__file__)])
    places = [f"{item['file']}:{item['line']}「{item['rule']}」" for item in findings[:5]]
    assert not findings, f"闸的源码或测试自身含完整泄露串：{places}"


def test_tracked_tree_is_clean_at_head():
    """发布门槛：整棵 tracked 树零命中（口径与 git grep 一致）。"""
    scanner = _scanner()
    files = scanner.iter_tracked_files()
    assert files, "tracked 清单为空，说明取文件的路径失效了（假绿）"
    assert any(p.suffix.lower() == ".py" for p in files), "tracked 清单里没有 *.py（扫描面不足）"
    findings = scanner.scan_files(files)
    places = [f"{item['file']}:{item['line']}「{item['rule']}」" for item in findings[:8]]
    assert not findings, f"公开面有命中：{places}"
