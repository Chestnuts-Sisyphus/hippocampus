"""N5 pending TTL＋`memory review`（D3/D4）。

验收口径（缺口清单 N5）：
- `pending_blocks` 加 TTL（默认 7 天）：超时保留旧值 + 记"未决冲突"；
- `memory review --pending`／`--candidates`／`--suspicious` 三个视图可用；
- 测试覆盖"超时后旧值仍生效且可查"。
"""

from __future__ import annotations

import pytest

from hippocampus.core import Scope
from hippocampus.core.core import PENDING_TTL_MS
from hippocampus.memory import database as db


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def _make_pending_conflict(core, scope):
    """造一条挂起确认：同一对象两个取值（规则冲突 → candidate + pending 块）。"""
    old = core.write(scope, "我的期望城市是杭州", kind="preference", explicit=True)
    new = core.write(scope, "我的期望城市是北京", kind="preference", explicit=False)
    assert new.pending, f"应挂起确认，实际 note={new.note} ids={new.ids}"
    return old.ids[0], new.ids[0]


def test_ttl_expiry_keeps_old_value(core, scope):
    """TTL 超时：旧值保持生效且可查；块记"未决冲突"；新候选保持 candidate。"""
    old_id, new_id = _make_pending_conflict(core, scope)

    # 未超时：pending 可见
    assert core.pending(scope)

    # 回拨 created_at 到 TTL 之前（8 天前），然后清一次
    session = core._session(scope)  # noqa: SLF001
    cutoff = db.now_ms() - (PENDING_TTL_MS + 86400000)
    session.conn.execute("UPDATE pending_blocks SET created_at=? WHERE resolved_at IS NULL", (cutoff,))
    session.conn.commit()

    expired = core.pending(scope)
    assert not expired  # 过期块不再出现在 pending

    # 旧值仍生效且可查
    items = core.list_memories(scope, status=None, include_shadow=True)
    contents = {i.id: i.content for i in items}
    assert contents[old_id] == "我的期望城市是杭州"
    old = core.get_memory(scope, old_id)
    assert old is not None and old.status == "active"
    new = core.get_memory(scope, new_id)
    assert new is not None and new.status == "candidate"  # 新值保持候选未生效

    # 块被记"未决冲突"
    row = session.conn.execute(
        "SELECT reason, resolved_at FROM pending_blocks WHERE resolved_at IS NOT NULL AND reason LIKE '%TTL 未决冲突%'"
    ).fetchone()
    assert row is not None


def test_review_pending_shows_ttl(core, scope):
    """`memory review --pending` 视图：块 + TTL 剩余。"""
    _make_pending_conflict(core, scope)
    blocks = core.pending_blocks(scope)
    assert blocks
    blk = blocks[0]
    assert blk["block_id"]
    assert blk["ttl_ms"] == PENDING_TTL_MS
    assert 0 < blk["ttl_remaining_ms"] <= blk["ttl_ms"]
    assert blk["entries"]


def test_review_candidates_view(core, scope):
    """`memory review --candidates`：candidate 状态记忆可见。"""
    _old, new_id = _make_pending_conflict(core, scope)
    items = core.list_memories(scope, limit=50, status="candidate", include_shadow=True)
    ids = [i.id for i in items]
    assert new_id in ids


def test_review_suspicious_view(core, scope):
    """`memory review --suspicious`：含候选 + TTL 未决冲突。"""
    _old, _new = _make_pending_conflict(core, scope)
    # 回拨 TTL 造未决冲突
    session = core._session(scope)  # noqa: SLF001
    cutoff = db.now_ms() - (PENDING_TTL_MS + 86400000)
    session.conn.execute("UPDATE pending_blocks SET created_at=? WHERE resolved_at IS NULL", (cutoff,))
    session.conn.commit()

    rows = core.suspicious(scope)
    flags = set()
    for r in rows:
        flags.update(r["flags"])
    assert any("待确认候选" in f for f in flags)
    assert any("TTL 未决冲突" in f for f in flags)
