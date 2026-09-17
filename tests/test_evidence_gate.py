"""证据闸的口径：**只有语义／关键词通道的分数能授权作答**。

事故背景（间歇性测试失败）：问一句与记忆无关的话（如"帮我写一首关于秋天的诗"），
Agent 偶尔会作答而不是拒答。原因是**事件线索通道**——它是**规则命中**（线索词／会话／
实体／时间），命中即给固定高分；问句里只要带了会话指代词或撞上本会话的实体，
就能捞到一堆本会话的经历，于是被判成"有依据"。

口径（本文件钉住）：
- 可授权作答的通道只有 `semantic`（向量）与 `bm25`（关键词）——它们的分数是**内容相关性**度量；
- `graph`（离散分 + 会话实体兜底）与 `event`（规则命中给固定分）**捞出来的可以注入**
  （那是"跨会话接着做"的能力），但**不构成依据**；
- 两条闸都要过：分数 ≥ 证据线，且与问题有实义关键词重合。
"""

from __future__ import annotations

from hippocampus.agent.policy import _EVIDENCE_CHANNELS, RulePolicy


def _policy() -> RulePolicy:
    return RulePolicy()


def test_only_relevance_channels_can_authorize():
    assert _EVIDENCE_CHANNELS == frozenset({"semantic", "bm25"})


def test_event_channel_high_score_is_not_evidence():
    """事件通道给满分也不算依据（规则命中 ≠ 回答了问题）。"""
    policy = _policy()
    policy._last_query = "帮我写一首关于秋天的诗"  # noqa: SLF001
    lines = ["[preference] 我只投含 MCP 的岗位"]
    assert policy.has_evidence(lines, [(1.0, "event")]) is False


def test_graph_channel_is_not_evidence():
    policy = _policy()
    policy._last_query = "我投简历有什么要求"  # noqa: SLF001
    assert policy.has_evidence(["[preference] 我只看允许远程的岗位"], [(0.5, "graph")]) is False


def test_semantic_hit_above_floor_is_evidence():
    policy = _policy()
    policy._last_query = "我投简历有什么要求"  # noqa: SLF001
    assert policy.has_evidence(["[preference] 简历投递前必须先过一遍错别字"], [(0.31, "semantic")]) is True


def test_bm25_hit_above_floor_is_evidence():
    policy = _policy()
    policy._last_query = "我的岗位筛选条件"  # noqa: SLF001
    assert policy.has_evidence(["[preference] 我只投含 MCP 的岗位"], [(0.30, "bm25")]) is True


def test_below_floor_is_not_evidence():
    policy = _policy()
    policy._last_query = "我投简历有什么要求"  # noqa: SLF001
    assert policy.has_evidence(["[preference] 简历投递前必须先过一遍错别字"], [(0.05, "semantic")]) is False


def test_overlap_required_even_with_good_score():
    """分数够但关键词不重合 → 仍不算依据（避免"捞到了但没回答这个问题"）。"""
    policy = _policy()
    policy._last_query = "我上次提到的那本书叫什么名字"  # noqa: SLF001
    assert policy.has_evidence(["[preference] 我只投含 MCP 的岗位"], [(0.42, "semantic")]) is False


def test_unrelated_question_refuses_end_to_end(core, scope, workdir):
    """端到端：与记忆无关的问题必须拒答——即使事件通道能从本会话捞到东西。"""
    from hippocampus.agent.runner import run_task
    from hippocampus.seed import seed

    seed(core, scope)
    # 先跑一轮，让本会话攒下经历与实体（这正是过去把事件通道喂饱、导致误判的路径）
    run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)
    run_task(core, scope, "我找岗位时有哪些硬性限制？", offline=True, workdir=workdir)

    run = run_task(core, scope, "帮我写一首关于秋天的诗", offline=True, workdir=workdir)
    assert run.exit == "cannot_complete", f"无关问题应拒答，实际 exit={run.exit}：{run.answer[:80]}"
    assert "创作" not in run.answer and "秋天" not in run.answer.replace("秋天的诗", "")
