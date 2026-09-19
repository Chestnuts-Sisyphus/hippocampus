"""九轮 W7：数字闸从"头条分"扩到**对照表**（缺口 K11／K12）。

八轮 V4 的闸只钉了三件事（头条分值集合、中英行集、xfailed 数），**没钉对照表**，
所以对照表里的数可以各写各的——本轮开工复核到的真实漂移：

- **K11 同一指标多值不标注归属**：`bge-base` 与 `bge-small` 各有两批全量数（批 A＝09-17 首轮 k=8；
  批 B＝同脚本同参复测 k=8／k=20），旧表里 `46.5%（另一轮 47.8%）` 把**别的臂**的数抄到了
  "＋官方英文查询指令"这一行；`docs/embedding.md` 还有一个仓内**无任何出处**的 `46.3%`。
- **K12 成对基线跨批配对**：写入 p50 的"修复前/后"有两批（批 C＝09-17 §6.1 表 748.6→42.0；
  批 D＝09-18 五轮 T6 632.8→41.98），旧 README 把 42 ms 与 632.8 ms 并排写却没说批次。

本轮先**订正归属**（见 `docs/benchmark.md` 的 A/B 表、`docs/embedding.md`、`docs/roadmap.md` §五、
`docs/release-sync.md` §三），再上这条闸：**同一个指标出现 >1 个数值时，每个值都必须带
（批次／规模／臂）标注**，且这些值必须等于登记在案的基线值。植入一个未登记的数（如 `bge-base=50.0%`）必红。

`docs/roadmap.md` 仍**不参与**"头条分"比对（旧口径表不与 README 增量口径混写，八轮 V4 口径不变）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENCH = REPO / "docs" / "benchmark.md"
ROADMAP = REPO / "docs" / "roadmap.md"
EMBEDDING = REPO / "docs" / "embedding.md"

# 登记在案的对照表基线：(臂, 批次, 注入条数) -> 证据命中率
EVIDENCE_RECALL = {
    ("bge-small-en-v1.5", "批 A", "k=8"): "45.7",
    ("bge-small-en-v1.5", "批 B", "k=8"): "45.2",
    ("bge-small-en-v1.5", "批 B", "k=20"): "49.8",
    ("bge-base-en-v1.5", "批 A", "k=8"): "47.8",
    ("bge-base-en-v1.5", "批 B", "k=8"): "46.5",
    ("bge-base-en-v1.5", "批 B", "k=20"): "48.8",
    ("builtin-hash", "批 A", "k=8"): "36.7",
    ("builtin-hash", "批 A", "k=20"): "37.2",
}

# 写入 p50 的成对基线：批次 -> (修复前, 修复后)
WRITE_P50_MS = {
    "批 C": ("748.6", "42.0"),
    "批 D": ("632.8", "41.98"),
}

_PERCENT = re.compile(r"(\d{2}\.\d)%")


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def unregistered_percentages(text: str, *, registered: set[str]) -> list[str]:
    """返回"出现但没登记"的百分数（对照表口径）。"""
    return sorted({v for v in _PERCENT.findall(text) if v not in registered})


@pytest.fixture(scope="module")
def bench_text() -> str:
    return BENCH.read_text(encoding="utf-8")


def test_bge_ab_table_values_match_the_registry(bench_text: str):
    """每个 (臂, 批次, k) 的登记值都必须在表里，且同一行/同一段里带臂名与批次标注。"""
    paras = _paragraphs(bench_text)
    for (arm, batch, k), value in EVIDENCE_RECALL.items():
        hit = [p for p in paras if arm in p and value + "%" in p]
        assert hit, f"{arm} 的 {batch}／{k} 值 {value}% 在 benchmark.md 里找不到（被改了或漏登记）"
        para = hit[0]
        assert batch in para, f"{arm} {value}% 没带批次标注（{batch}）——同一指标多值必须可归属"
        assert k in para, f"{arm} {value}% 没带规模／注入条数标注（{k}）"


def test_no_unsourced_percentage_in_ab_table_region(bench_text: str):
    """对照表区段里不得出现"登记之外"的百分数（K11 的病灶：46.5 被抄到别的臂、46.3 无出处）。"""
    start = bench_text.find("**同一指标有多个数值时")
    assert start > 0, "对照表的批次口径说明被删了（闸失去锚点）"
    block = bench_text[start:start + 2500]
    extras = unregistered_percentages(block, registered=set(EVIDENCE_RECALL.values()) | {"33.8", "41.3", "27.5", "32.5"})
    assert not extras, f"对照表出现未登记的百分数：{extras}（要么补登记＋批次，要么删掉）"


def test_embedding_doc_dropped_the_unsourced_number():
    """`docs/embedding.md` 里那个无出处的 46.3% 已换成有据的两批配对（K11 订正落地）。"""
    text = EMBEDDING.read_text(encoding="utf-8")
    assert "46.3" not in text, "无出处的 46.3% 又回来了"
    assert "46.5% vs 45.2%" in text and "47.8% vs 45.7%" in text


def test_write_p50_pairs_are_batch_labelled(bench_text: str):
    """写入 p50 的"修复前/后"必须**同批成对**并点明批次（K12）。"""
    roadmap = ROADMAP.read_text(encoding="utf-8")
    for batch, (before, after) in WRITE_P50_MS.items():
        rows = [ln for ln in roadmap.splitlines() if batch in ln and before in ln and after in ln]
        assert rows, f"roadmap 里 {batch} 的成对基线（{before}→{after}）不见了或被拆散"
    # 批内配对不得跨批混写
    mixed = [ln for ln in roadmap.splitlines() if "632.8" in ln and "42.0 ms" in ln and "批" not in ln]
    assert not mixed, f"跨批配对未标批次：{mixed}"


def test_roadmap_number_section_stays_out_of_headline_scores():
    """旧口径表不与 README 增量口径混写（八轮 V4 口径不变）：`docs/roadmap.md` §六"实测数字"
    那一节里**不得**出现头条分 74.2%（§一/§三的已解决叙事里引用是允许的）。"""
    text = ROADMAP.read_text(encoding="utf-8")
    start = text.find("## 六、实测数字")
    assert start > 0, "roadmap §六 定位不到（闸失去作用域）"
    end = text.find("## 七、", start)
    section = text[start:end if end > 0 else len(text)]
    assert "74.2" not in section, "roadmap 的实测数字节混进了 README 的头条分口径（两套口径不得并表）"


def test_gate_turns_red_on_a_planted_table_value(bench_text: str):
    """对照测试（植入即红）：把 bge-base 的批 B k=8 换成 50.0% → 闸必须报出未登记值。"""
    planted = bench_text.replace('46.5%／48.8%（批 B，k=8／k=20）', '50.0%／48.8%（批 B，k=8／k=20）')
    assert planted != bench_text, "植入未命中（表格式变了，本闸需要随动更新）"
    start = planted.find("**同一指标有多个数值时")
    block = planted[start:start + 2500]
    extras = unregistered_percentages(block, registered=set(EVIDENCE_RECALL.values()) | {"33.8", "41.3", "27.5", "32.5"})
    assert "50.0" in extras, "植入 bge-base=50.0% 未被逮住"
