"""T7 ±1 轮邻居扩展（A1）的验收测试。

验收口径（任务书）：
- 邻居存在 → 追加；邻居不存在（首轮/末轮）→ 只给在的那边；
- **跨会话不越界**（LoCoMo 多 session 对话，邻居必须同 session）；
- 预算裁剪（`neighbor_budget` 只裁追加量）；
- 已在上下文的邻居不重复追加；
- 端到端赢场景：证据轮没被检索、但其邻居进了上下文 → 扩展把证据带回来；
- 默认关＝零变化（跑批不传参数时行为与旧版一致）。
"""

from __future__ import annotations

from hippocampus.eval.public_bench import (
    Turn,
    neighbor_texts,
    neighbor_turn_indices,
    normalize_text,
)


def _turns(*texts: tuple[str, str]) -> list[Turn]:
    """每项 (session_id, text)。"""
    return [
        Turn(text=txt, role="user", ts_ms=1000 + i, session_id=sid)
        for i, (sid, txt) in enumerate(texts)
    ]


def test_expands_prev_and_next_of_injected_turn():
    """已注入轮 i → ±1 邻居都追加（且按跳顺序）。"""
    turns = _turns(("s1", "第一轮：吃早饭"), ("s1", "第二轮：开会"), ("s1", "第三轮：写代码"))
    extra = neighbor_texts(turns, [1], context_norm="", budget=6)
    assert [t.text for t in extra] == ["第一轮：吃早饭", "第三轮：写代码"]


def test_first_turn_only_next_neighbor():
    """首轮：只有下一轮（没有上一轮可扩）。"""
    turns = _turns(("s1", "开头"), ("s1", "中间"), ("s1", "结尾"))
    extra = neighbor_texts(turns, [0], context_norm="", budget=6)
    assert [t.text for t in extra] == ["中间"]


def test_session_boundary_not_crossed():
    """跨会话不越界：会话边界两侧的轮不是邻居。"""
    turns = _turns(
        ("s1", "会话一的最后一句"),
        ("s2", "会话二的开始"),      # 与上一句同下标相邻但不同 session
        ("s2", "会话二的第二句"),
    )
    # 只注入会话二第一句：上一轮（会话一末句）不得被当成邻居
    extra = neighbor_texts(turns, [1], context_norm="", budget=6)
    assert [t.text for t in extra] == ["会话二的第二句"]


def test_budget_trims_appended_neighbors():
    """预算裁剪：只裁追加量（前 budget 条，按注入轮顺序）。"""
    turns = _turns(*(("s1", f"轮{i}") for i in range(6)))
    extra = neighbor_texts(turns, [1, 3], context_norm="", budget=2)
    assert len(extra) == 2  # 1 的上下邻居先到：轮0、轮2


def test_already_in_context_not_duplicated():
    """已在上下文的邻居不重复追加。"""
    turns = _turns(("s1", "轮一"), ("s1", "轮二"), ("s1", "轮三"))
    # context 已含"轮三"（邻居之一）→ 只追加"轮一"
    extra = neighbor_texts(turns, [1], context_norm=normalize_text("轮三"), budget=6)
    assert [t.text for t in extra] == ["轮一"]


def test_turn_index_map_handles_date_prefix():
    """内容→轮下标映射：含日期前缀与不含两态都能命中。"""
    turns = _turns(("s1", "我们把预算砍半了"))
    turns[0].ts_ms = 1683564800000  # 2023-05-08
    idx = neighbor_turn_indices(turns)
    assert idx[normalize_text("我们把预算砍半了")] == 0
    assert idx[normalize_text("[2023-05-08] 我们把预算砍半了")] == 0


def test_evidence_recovered_via_neighbor():
    """赢场景：证据轮没进上下文，但其邻居进了 → 扩展把证据带回来。"""
    turns = _turns(
        ("s1", "今天天气怎么样"),
        ("s1", "答案藏在第二轮的笔记本里"),   # 证据轮（假设没被检索）
        ("s1", "我们顺便确认了明天的安排"),
    )
    # 只有第 2 轮被注入（它是证据轮的下一轮）；邻居扩展应把证据轮（第 1 轮）带回来
    extra = neighbor_texts(turns, [2], context_norm="", budget=6)
    texts = [t.text for t in extra]
    assert "答案藏在第二轮的笔记本里" in texts


def test_empty_injected_indices_adds_nothing():
    """没有注入轮就不扩展（空安全）。"""
    turns = _turns(("s1", "a"), ("s1", "b"))
    assert neighbor_texts(turns, [], context_norm="", budget=6) == []
