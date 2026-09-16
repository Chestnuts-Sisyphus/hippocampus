"""验收 A29 / T5-①：**去重不再吞掉同构对立变更句**（P1 修复的核心断言）。

前身缺陷（数据丢失级，前身实测 8/14）：语义去重命中后，机械判据拿不准就 `drop`
把新句丢掉——用户改主意永远不生效（「深色→浅色」"VS Code→Vim"这类）。

修复口径（方案 §15.3-④）：命中后**只走三条路**，一条都不许静默丢：
  supersede（确认值变更）／admit（同构换值，两条都留）／挂起确认（候选入库 + 待裁决）。
"""

from __future__ import annotations

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import dedup


def _contents(core: MemoryCore, scope: Scope) -> list[tuple[str, str]]:
    items = core.list_memories(scope, limit=50, status=None, include_shadow=True)
    return [(i.content, i.status) for i in items]


def _force_semantic_hit(monkeypatch, old_id: str) -> None:
    """把"语义去重命中旧条"钉死。

    为什么必须钉：向量索引写入是异步的（chroma 队列），靠真实检索来触发去重路径在
    快速连续写入时不稳定——测试要验的是**命中之后的处置**（supersede／admit／挂起），
    不是"索引多久写完"。命中判定本身由 test_classifier_never_returns_silent_drop 覆盖。
    """
    monkeypatch.setattr(
        "hippocampus.core.core.MemoryCore._semantic_dup",
        lambda self, session, kind, text, session_id: old_id,
    )


def test_polarity_flip_is_superseded_not_dropped(core, scope, monkeypatch):
    """极性翻转（喜欢 ↔ 不喜欢）：必须被取代，不能被丢弃。"""
    old = core.write(scope, "我喜欢深色主题", kind="preference").ids[0]
    _force_semantic_hit(monkeypatch, old)
    result = core.write(scope, "我不喜欢深色主题", kind="preference")

    assert result.ids, "变更句被丢弃了（P1 回归）"
    assert result.superseded, "极性问题应走 supersede"
    rows = _contents(core, scope)
    assert ("我不喜欢深色主题", "active") in rows
    assert ("我喜欢深色主题", "superseded") in rows


def test_isomorphic_value_swap_is_admitted(core, scope, monkeypatch):
    """同构换值（VS Code → Vim）：两条都要留下（旧条不被取代）。"""
    old = core.write(scope, "我用 VS Code 写代码", kind="preference").ids[0]
    _force_semantic_hit(monkeypatch, old)
    result = core.write(scope, "我用 Vim 写代码", kind="preference")

    assert result.ids, "同构换值句被丢弃了"
    assert not result.superseded, "同构换值不应取代旧条（用户没说旧的错）"
    rows = dict(_contents(core, scope))
    assert rows.get("我用 VS Code 写代码") == "active"
    assert rows.get("我用 Vim 写代码") == "active"


def test_paraphrase_is_never_dropped(core, scope, monkeypatch):
    """改写型重复：允许两条都在（宁冗余不丢变更）。"""
    old = core.write(scope, "我讨厌打补丁式设计", kind="preference").ids[0]
    _force_semantic_hit(monkeypatch, old)
    monkeypatch.setattr("hippocampus.memory.dedup.classify_dup_action", lambda *_a, **_k: "drop")
    result = core.write(scope, "我讨厌打补丁式的设计，要浑然天成", kind="preference")

    assert result.ids, "改写句被丢弃了"
    assert result.pending, "判据不确定时应挂起而不是丢弃"
    rows = dict(_contents(core, scope))
    # 判据不确定的改写型：新句以候选身份入库（不丢），旧句在裁决前仍生效
    assert rows.get("我讨厌打补丁式的设计，要浑然天成") == "candidate"
    assert rows.get("我讨厌打补丁式设计") == "active"


def test_uncertain_hit_goes_to_pending_candidate(core, scope, monkeypatch):
    """判据拿不准的那一档：新条以 candidate 入库并挂起确认，旧值在裁决前仍然生效。"""
    old = core.write(scope, "我更喜欢 A 方案", kind="preference").ids[0]

    # 强制让语义去重命中旧条，并把机械判据压到"drop"档（模拟最坏情形）
    _force_semantic_hit(monkeypatch, old)
    monkeypatch.setattr(
        "hippocampus.memory.dedup.classify_dup_action",
        lambda *_a, **_k: "drop",
    )

    result = core.write(scope, "我更喜欢 B 方案", kind="preference")
    assert result.ids, "判据不确定时不允许丢弃"
    assert result.pending, "应挂起待确认"
    assert result.note, "应写明'已挂起待确认（不丢弃）'"

    rows = dict(_contents(core, scope))
    assert rows.get("我更喜欢 A 方案") == "active", "旧值在裁决前必须保持生效"
    assert rows.get("我更喜欢 B 方案") == "candidate", "新值以候选身份等待裁决"

    # 候选不参与注入，也不参与后续去重基准
    injected = core.inject_finalize(scope, "我更喜欢哪个方案")
    assert all("我更喜欢 B 方案" not in i.content for i in injected.items)


def test_candidate_confirmed_becomes_active(core, scope, monkeypatch):
    """挂起后确认：候选转正、旧条被取代（不删）。"""
    old = core.write(scope, "我更喜欢 A 方案", kind="preference").ids[0]
    _force_semantic_hit(monkeypatch, old)
    monkeypatch.setattr("hippocampus.memory.dedup.classify_dup_action", lambda *_a, **_k: "drop")
    core.write(scope, "我更喜欢 B 方案", kind="preference")

    pending = core.pending(scope)
    assert pending, "应有未决确认"
    candidate = next(p for p in pending if p["is_new"])
    outcome = core.confirm(scope, f"确认{candidate['num']}")
    assert outcome is not None and outcome.decision == "confirm"

    rows = dict(_contents(core, scope))
    assert rows.get("我更喜欢 B 方案") in ("active", "candidate")  # candidate → 去掉候选标记
    assert rows.get("我更喜欢 A 方案") == "superseded"


def test_classifier_never_returns_silent_drop_for_real_changes():
    """机械判据本身的边界：真变更句不许落进 drop。"""
    assert dedup.classify_dup_action("我喜欢 42 码的鞋", "我喜欢 40 码的鞋") == "supersede"
    assert dedup.classify_dup_action("我喜欢深色主题", "我不喜欢深色主题") == "supersede"
    assert dedup.classify_dup_action("我用 VS Code", "我用 Vim") == "admit"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("我的期望城市是北京", "我的期望城市是杭州"),
        ("我每周做两次运动", "我每周做三次运动"),
        ("我接受远程办公", "我不接受远程办公"),
    ],
)
def test_change_sentences_survive(core, scope, monkeypatch, old, new):
    """成对变更句都不许丢（参数化覆盖常见改口形态）。"""
    old_id = core.write(scope, old, kind="preference").ids[0]
    _force_semantic_hit(monkeypatch, old_id)
    result = core.write(scope, new, kind="preference")
    assert result.ids, f"'{new}' 被丢弃了"
    assert any(c == new for c, _ in _contents(core, scope)), f"'{new}' 不在库里"
