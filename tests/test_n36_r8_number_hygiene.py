"""八轮 V4：把"基准数字跨文档同源"从人肉对齐升级成闸。

起因（七轮深夜实测）：README 数字实时化时**五处**要手工对齐（README 双语／`docs/benchmark.md`／
`benchmark.en.md`／`docs/roadmap.md`），当时的守护测试 `test_n32_r7_docs_drift.py` 只钉了
"README 总数 == collect 真值"与"双语头牌一致"，**没钉**跨文档分值集合、没钉 xfailed 数、
也没钉中英两版结果表的行集——漏改一处就是文档里两个数并存（本轮七轮补的那处缺失结果表即此症状）。

三条断言（口径："允许各文件子集，但不得互相矛盾"）：
1. **同源不矛盾**：四份文档里每个指标位置解析出的分值，必须落在该指标的合法集合内
   （新口径 74.2 与留作历史的 70.5 都允许；出现第三值 = 有人手改了一个数）；
2. **中英结果表行集一致**：`benchmark.md` §二·三 与 `benchmark.en.md` §4 的官方判分结果表，
   行数与（行身份 → 官方分）序列必须完全相同——防止"只改一版"；
3. **xfailed 数同源**：README 声明的 xfailed 条数 == `tests/` 里 `pytest.mark.xfail` 的实际数量。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "README.zh-CN.md", "docs/benchmark.md", "docs/benchmark.en.md", "docs/roadmap.md")

NUM = r"\d+(?:\.\d+)?"

# 指标 → (定位正则, 合法分值集合)。定位正则只吃"指标名→判分口径→分值"这一段，
# 且窗口里禁止 `%` 与换行，避免同类的检索率／账户数被当成官方分（第一轮实跑就踩了）。
# 合法集合里同时放行**新口径**与**历史口径**（70.5 是 n=200 抽样批，README 已降为历史行），
# 也放行 CI 区间端点与"修复前对照值"——闸拦的是"冒出第三个口径外的数"。
# `(?<![\d≥=])`：发布门槛（LME ≥50%、LoCoMo F1 ≥15%）是**判据**不是实测分值，不能混进同一集合
# （挡 ≥ 的同时还得挡前一个数字，否则 `≥50%` 会被退化成匹配尾部的 `0%`）。
METRIC_RULES: dict[str, tuple[re.Pattern[str], set[str]]] = {
    "LongMemEval 官方分": (
        re.compile(
            rf"(?:LongMemEval|LME)[^\n%]{{0,80}}?(?:accuracy|准确率|官方分|判分)[^\n%]{{0,40}}?(?<![\d≥=])({NUM})\s*%"
        ),
        {"74.2", "70.5", "70.2", "77.8", "63.8", "76.4"},
    ),
    "LoCoMo 官方 F1": (
        re.compile(rf"LoCoMo[^\n%]{{0,80}}?F1[^\n%]{{0,20}}?(?<![\d≥=])({NUM})\s*%"),
        {"32.55", "38.68", "30.8", "34.4", "36.8", "40.5"},
    ),
    "写入延迟 p50": (
        # 中文分支不跨行；英文分支允许跨一次行（README 里 "single write" 与 p50 被换行拆开）
        re.compile(rf"(?:(?:单条写入|写入)[^%\n]{{0,30}}|(?i:write)[^%]{{0,40}})p50[^%\n]{{0,14}}?({NUM})\s*(?:ms|毫秒)"),
        {"42.0", "42", "632.8", "749"},
    ),
    "内存峰值": (
        re.compile(rf"({NUM})\s*GB"),
        {"1.1", "4"},
    ),
}


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_each_metric_has_one_value_set_across_docs():
    """断言 1：四份文档里同一指标只允许出现合法集合内的分值（不得互相矛盾）。"""
    offenders: list[str] = []
    for rel in DOCS:
        text = _text(rel)
        for metric, (pattern, allowed) in METRIC_RULES.items():
            for value in pattern.findall(text):
                if value not in allowed:
                    offenders.append(f"{rel} [{metric}] 出现 {value}（合法集合 {sorted(allowed)}）")
    assert not offenders, "基准分值跨文档不同源：\n  " + "\n  ".join(offenders)


def test_the_rules_catch_a_planted_contradiction():
    """对照（防闸被改松后静默假绿）：植入三个口径外分值，每条规则都必须各自报出来。"""
    planted = (
        "| LongMemEval-oracle | LLM-judged accuracy (official judge prompts) | **61.3%** | 500 |\n"
        "| LoCoMo-10 (n=1986) | **F1 41.11%** | 45.5% / 17.4% |\n"
        "单条写入 p50 **77 ms**（增量索引同步）\n"
    )
    for metric in ("LongMemEval 官方分", "LoCoMo 官方 F1", "写入延迟 p50"):
        pattern, allowed = METRIC_RULES[metric]
        stray = [v for v in pattern.findall(planted) if v not in allowed]
        assert stray, f"规则「{metric}」拦不住植入的矛盾分值（闸被改松了）"


# 只有这四份文档承载"官方判分增量口径"；roadmap.md 不参与（它的 §6.1 是**旧口径**性能表，
# 按口径隔离纪律不得混写当前分值，硬要它出现 74.2 反而制造混写）。
HEADLINE_DOCS = ("README.md", "README.zh-CN.md", "docs/benchmark.md", "docs/benchmark.en.md")


def test_headline_scores_are_all_present_in_docs_that_claim_them():
    """断言 1b：只要一份文档给了官方判分口径，就必须给出当前头条值。"""
    for rel in HEADLINE_DOCS:
        text = _text(rel)
        if re.search(r"LongMemEval|LME", text):
            assert "74.2" in text, f"{rel} 提到 LongMemEval 却没有当前头条 74.2（头号数字漏改）"
        if re.search(r"LoCoMo", text) and "F1" in text:
            assert "38.68" in text, f"{rel} 提到 LoCoMo 官方 F1 却没有当前值 38.68"


# 结果表：行身份由"基准 + 样本量 + 是否邻居臂"确定，与中英文标签无关（语言无关的行集比较）
RESULT_TABLE_ROWS = [
    ("lme", "500", False, "74.2"),
    ("lme", "200", False, "70.5"),
    ("loco", "1986", False, "32.55"),
    ("loco", "1986", True, "38.68"),
]

RESULT_HEADER = re.compile(r"官方分|Official score")


def _result_row_index(text: str) -> list[tuple[str, str, bool, str]]:
    """在含"官方分（95% CI）"／"Official score (95% CI)"表头的表格中，按行取（基准, 样本量, 邻居臂, 官方分）。"""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("|") and RESULT_HEADER.search(ln)), None)
    assert start is not None, "没找到官方判分结果表（表头应含『官方分（95% CI）』或『Official score (95% CI)』）"
    rows: list[tuple[str, str, bool, str]] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        joined = " ".join(cells)
        bench = "lme" if re.search(r"LongMemEval|LME", joined, re.IGNORECASE) else "loco"
        size = next((m for m in re.findall(r"(\d{3,4})", joined) if m in {"500", "200", "1986"}), "?")
        neighbors = bool(re.search(r"neighbors|邻居", joined, re.IGNORECASE))
        # 官方分＝该行第一个带 % 的分值（CI 区间写在它后面）
        score = next(iter(re.findall(rf"({NUM})(?=\s*%)", joined)), "?")
        rows.append((bench, size, neighbors, score))
    return rows


def test_cn_and_en_benchmark_result_tables_have_same_rows():
    """断言 2：中英两版结果表的行集与官方分序列必须一致（防"只改一版"）。"""
    zh = _result_row_index(_text("docs/benchmark.md"))
    en = _result_row_index(_text("docs/benchmark.en.md"))
    assert zh == en == RESULT_TABLE_ROWS, f"结果表行集不一致：zh={zh} en={en} 正本={RESULT_TABLE_ROWS}"


XFAILED_IN_README = re.compile(r"(\d+)\s*xfailed")


def test_readme_declared_xfailed_count_matches_markers():
    """断言 3：README 声明的 xfailed 数 == tests/ 里实打的 xfail 装饰器数。

    只认行首装饰器（本闸文件自己写着规则说明，不算），且排除本文件——否则"讲规则的注释"会被当成"打标记的测试"。
    """
    declared = set(XFAILED_IN_README.findall(_text("README.md")))
    assert len(declared) == 1, f"README 里 xfailed 声明应只有一处，实得 {declared}"
    decorator = re.compile(r"^\s*@pytest\.mark\.xfail")
    actual = sum(
        1
        for path in (REPO / "tests").rglob("test_*.py")
        if path != Path(__file__)
        for line in path.read_text(encoding="utf-8").splitlines()
        if decorator.match(line)
    )
    assert int(declared.pop()) == actual, f"README 声明 xfailed 数与实打标记数 {actual} 不符"
