"""N8 评测升级（C1–C4）：20 题＋pass^k＋模型臂＋关键词基线对照。

验收口径（缺口清单 N8）：
- 20 题（含真实 JD 来源标注）；
- `pass^k`（k≥3）；
- 模型臂（有端点时）；
- 关键词基线对照；
- 报告分列"合成数据/真实数据"；边界声明含 k 与模型名。
"""

from __future__ import annotations

import pytest

from hippocampus.core import Scope
from hippocampus.eval.questions import QUESTIONS, questions_for


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def test_questions_total_20_with_sources():
    """题集 20 题；含 real-jd 来源标注；QA:TOOL ≈ 7:3。"""
    assert len(QUESTIONS) == 20
    qa = [q for q in QUESTIONS if q.kind == "qa"]
    tool = [q for q in QUESTIONS if q.kind == "tool"]
    assert len(qa) == 14 and len(tool) == 6
    real = [q for q in QUESTIONS if q.source == "real-jd"]
    assert len(real) >= 3, "应有真实 JD 派生题（脱敏）"
    assert questions_for(20) == QUESTIONS
    assert len(questions_for(10)) == 10


def test_eval_report_separates_sources_and_boundary(core, scope):
    """报告分列来源；边界声明含 k 与模型名。"""
    from hippocampus.eval.runner import run_bundle

    report = run_bundle(core, scope, questions=20, memories=False, baseline=True, pass_k=1)
    assert report.arms
    on_arm = report.arms[0]
    counts = on_arm.source_counts
    assert counts.get("synthetic", 0) > 0 and counts.get("real-jd", 0) > 0
    assert str(report.pass_k) in report.boundary
    assert "模型臂" in report.boundary
    # 关键词基线臂存在
    names = [a.name for a in report.arms]
    assert "关键词基线" in names


def test_pass_k_repeat_runs(core, scope):
    """pass^k：每题重复 k 次全过才算过，报 pass^k 分数。"""
    from hippocampus.eval.runner import run_bundle

    report = run_bundle(core, scope, questions=5, pass_k=3)
    arm = report.arms[0]
    assert arm.pass_k == 3
    assert 0.0 <= arm.pass_k_score <= 1.0
    # 每题 note 里带 pass^k 记录
    assert any("pass^3" in r.note for r in arm.results)


def test_model_arm_skipped_offline(core, scope):
    """离线档请求模型臂 → 跳过并注明（CI 无 key 路径）。"""
    from hippocampus.eval.runner import run_bundle

    report = run_bundle(core, scope, questions=5, model_arm=True, offline=True)
    names = [a.name for a in report.arms]
    assert not any("模型臂" in n for n in names)
    assert any("模型臂跳过" in n for n in report.notes)


def test_keyword_baseline_arm_semantics(core, scope):
    """关键词基线臂：只按 BM25 直查库（与四通道融合形成对照）。"""
    from hippocampus.eval.runner import run_bundle

    report = run_bundle(core, scope, questions=20, baseline=True)
    base = next(a for a in report.arms if a.name == "关键词基线")
    assert base.total == 20
    # 基线结果标注了口径
    assert all("关键词基线" in r.answer for r in base.results)
