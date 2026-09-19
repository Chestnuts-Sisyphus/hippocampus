"""九轮 W8：文档引用卫生轻闸（缺口 K13／K18）+ 互斥表述订正。

现象（本轮开工逐条复核到的四类）：
1. `docs/benchmark.md` 同一节里既写"人工裁决未做"又写"人工抽判 20 题"（互斥表述，已订正为
   "双判式人工对齐未做／单向人工抽判已做"）；
2. 文档里 `X.md §Y` 形式的**章节引用指错**（如指到 `docs/roadmap.md §30`、`CHANGELOG.md §374`
   这类根本不存在的章节号）；
3. `docs/release-sync.md` §三 的同步记录停在五轮（已改成按轮次分节）；
4. 仓外文件的引用只剩"文件还在不在"可验（章节号无法验，也不该硬编码进公开仓）。

本文件因此只钉两条硬规则：
- **仓内**文档的 `X.md §Y` 引用，`Y` 必须命中 X 的真实标题；
- **跨仓**引用（绝对路径／别仓文件名）**只校验文件存在**，不校验章节号。

外加"植入即红"对照：造一条指向不存在章节的引用，闸必须报出来。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# `docs/foo.md §二·五第 3 条`／`README.zh-CN.md §三`／`benchmark.md §6.1`
_REF = re.compile(r"([A-Za-z0-9_\-\.]+\.(?:md|py|yml))[^\n]{0,12}?§\s*([0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十]+(?:·[0-9一二三四五六七八九十]+)?|[①②③④⑤⑥⑦⑧⑨⑩])")
_HEADING = re.compile(r"^#{1,4}\s+(?:[一二三四五六七八九十]+、|[0-9]+(?:\.[0-9]+)*\s|①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)?([^\s，。、:：]+)")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md"], cwd=REPO, capture_output=True, timeout=60, check=True
    ).stdout
    return [REPO / raw.decode("utf-8", "replace") for raw in out.split(b"\0") if raw]


def heading_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("#"):
            continue
        body = line.lstrip("#").strip()
        keys.add(body)
        m = re.match(r"^(\d+(?:\.\d+)*)[\s、\.]", body)
        if m:
            keys.add(m.group(1))
        m = re.match(r"^([一二三四五六七八九十]+)(?:[、\.]|$)", body)
        if m:
            keys.add(m.group(1))
        m = re.match(r"^([一二三四五六七八九十]+·\d+)[、\.]", body)
        if m:
            keys.add(m.group(1))
        m = re.match(r"^([①②③④⑤⑥⑦⑧⑨⑩])", body)
        if m:
            keys.add(m.group(1))
        # "### 6.1 性能…" 这类
        m = re.match(r"^(\d+\.\d+)", body)
        if m:
            keys.add(m.group(1))
    return keys


def resolve_target(ref_file: str, source: Path) -> Path | None:
    """把 `docs/x.md`／`x.md` 解析成仓内路径；解析不到＝跨仓引用（只查存在性）。"""
    for cand in (REPO / ref_file, REPO / "docs" / ref_file, source.parent / ref_file):
        if cand.is_file():
            return cand.resolve()
    return None


def find_bad_refs(text: str, source: Path) -> list[str]:
    bad: list[str] = []
    for ref_file, section in _REF.findall(text):
        target = resolve_target(ref_file, source)
        if target is None:
            continue  # 跨仓引用：存在性由 test_cross_repo_files_exist 管
        keys = heading_keys(target)
        norm = section.rstrip("、.")
        if norm in keys:
            continue
        if not any(norm in k or k.startswith(norm) for k in keys):
            bad.append(f"{source.relative_to(REPO).as_posix()} → {ref_file} §{section}")
    return bad


def test_all_in_repo_section_references_resolve():
    bad: list[str] = []
    for path in tracked_markdown():
        bad.extend(find_bad_refs(path.read_text(encoding="utf-8", errors="replace"), path))
    assert not bad, "章节引用指错：\n  " + "\n  ".join(bad)


def test_gate_turns_red_on_a_planted_bad_reference():
    """对照测试（植入即红）：指向不存在章节的引用必须被逮住。"""
    planted = "口径见 `docs/roadmap.md §九十九` 与 `docs/deployment.md §二·五`。"
    hits = find_bad_refs(planted, REPO / "CHANGELOG.md")
    assert hits == ["CHANGELOG.md → roadmap.md §九十九"], f"植入的错引用未被逮住：{hits}"


@pytest.mark.parametrize("relpath", ["docs/benchmark.md", "docs/release-sync.md"])
def test_contradiction_fixes_are_in_place(relpath: str):
    """K13 的两处订正必须还在：benchmark 的"人工裁决未做"互斥表述、release-sync 停在五轮的 §三。"""
    text = (REPO / relpath).read_text(encoding="utf-8")
    if relpath == "docs/benchmark.md":
        assert "（人工裁决未做，不假装已做）" not in text
        assert "双判式人工对齐" in text and "单向人工抽判" in text
    else:
        assert "按轮次的同步记录" in text and "九轮（2026-09-19" in text
