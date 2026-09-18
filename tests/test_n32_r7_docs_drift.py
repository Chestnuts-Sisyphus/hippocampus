"""七轮 T7 守护：文档里的硬数字不许漂移（README 双语 ↔ 真实测量 ↔ roadmap 口径标注）。

踩坑实证（同类问题已犯三次）：README 写"421 条测试"而真值是 476，发到 v0.3.0 才被发现
（六轮 D1）；roadmap §6.1 的 748 ms 是**修复前**全量 re-sync 口径，与 README 的增量口径
混排就会被当成现行数字外发。所以把"数字有出处、口径有标注"钉成测试。

三条闸：
1. README 双语声明的测试条数 == `pytest --collect-only` 真值（**两处都查**）；
2. README 双语的头牌数字必须一致（同一份事实，两个语言版本不许各说各话）；
3. roadmap 的旧口径性能表必须带"旧口径"标注并指向现行增量口径。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README_EN = REPO_ROOT / "README.md"
README_ZH = REPO_ROOT / "README.zh-CN.md"
ROADMAP = REPO_ROOT / "docs" / "roadmap.md"


@pytest.fixture(scope="module")
def collected_count() -> int:
    """`pytest --collect-only` 的收集真值（分文件计数求和，与 README 口径一致）。"""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-p", "no:warnings"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # Windows 子进程 PIPE 必须显式声明编码，否则按 cp1252 解
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-500:] + proc.stderr[-500:]
    out = proc.stdout
    node_lines = [ln for ln in out.splitlines() if "::" in ln and ln.strip().startswith("tests/")]
    if node_lines:
        return len(node_lines)
    per_file = re.findall(r"^tests/\S+\.py:\s*(\d+)\s*$", out, re.MULTILINE)
    assert per_file, f"没能从收集输出里数出用例数：\n{out[-500:]}"
    return sum(int(n) for n in per_file)


def _declared_counts(path: Path) -> list[int]:
    text = path.read_text(encoding="utf-8")
    hits = re.findall(r"(?:(\d{3,4})\s*(?:pytest\s+tests|条|個|个))|(?:(\d{3,4})\s*条)", text)
    numbers = [int(a or b) for a, b in hits if (a or b)]
    assert numbers, f"{path.name} 里找不到声明的测试条数"
    return numbers


def test_readme_declared_test_count_matches_collection(collected_count: int):
    """README 两个语言版本声明的测试条数，都必须等于收集真值。"""
    for path in (README_EN, README_ZH):
        declared = _declared_counts(path)
        assert collected_count in declared, (
            f"{path.name} 声明的测试数是 {declared}，pytest 收集真值是 {collected_count} —— "
            "改代码加了测试就要同日改 README（双语两处），否则这条闸会一直红"
        )


def test_readme_pair_agrees_on_headline_numbers():
    """双语 README 的头牌数字必须同源（官方分＋写入延迟＋内存峰值＋增量口径）。"""
    en = README_EN.read_text(encoding="utf-8")
    zh = README_ZH.read_text(encoding="utf-8")
    for number in ("70.5", "32.55", "38.68", "1.1 GB"):
        assert number in en, f"英文 README 缺 {number}"
        assert number in zh, f"中文 README 缺 {number}"
    for text, name in ((en, "README.md"), (zh, "README.zh-CN.md")):
        assert re.search(r"42\s*ms", text), f"{name} 没有写入延迟 42 ms（增量口径）"
        assert re.search(r"632\.8", text), f"{name} 丢了修复前对照值 632.8 ms（两口径要并陈）"


def test_roadmap_legacy_perf_table_is_labelled():
    """roadmap §6.1 的全量 re-sync 性能表必须标注【旧口径】，并给出指向现行增量口径的数字。"""
    doc = ROADMAP.read_text(encoding="utf-8")
    section = doc.split("### 6.1", 1)[1].split("### ", 1)[0]
    assert "旧口径" in section, "§6.1 表题注缺『旧口径』标注"
    assert "748.6" in section, "§6.1 原表数字被改动（本闸只要求加标注，不动历史数字）"
    assert "42.0" in section, "§6.1 没指向现行增量口径（p50 42.0 ms）"


def test_no_hardcoded_scan_file_counts_in_docs():
    """扫描覆盖面／测试规模这类会随提交漂移的计数，文档里不许再硬编成"当前 N 文件"。"""
    offenders = []
    for path in [README_EN, README_ZH, *sorted((REPO_ROOT / "docs").glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        for pattern in (r"当前 \d+ 文件", r"over \d+ files", r"\d+ files.{0,6}zero hit"):
            for line in text.splitlines():
                if re.search(pattern, line):
                    offenders.append(f"{path.name}: {line.strip()[:80]}")
    assert not offenders, f"文档里有硬编码的扫描文件数：{offenders}"
