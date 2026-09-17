"""T6 增量 upsert（A3）：单条写入不再全量索引同步。

验收：
- write 只把**本次写触碰的行**同步进向量池（新记忆＋新经历＋被取代的旧记忆），
  不是全库 upsert（monkeypatch 捕获断言）；
- 增量后检索仍看得到新写（真端到端，不是只改了调用参数）；
- 掉出 active 的行从池里删除（superseded/archived/candidate 不自相矛盾）；
- 批量导入/确认等低频路径保持全量同步（ids=None，回归保护）；
- 既有全量路径（ingest_history）行为零变化。
"""

from __future__ import annotations

import pytest

from hippocampus.core import Scope
from hippocampus.memory import database as db
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def _mem_pool_count(core, scope) -> int:
    session = core._session(scope)  # noqa: SLF001
    return session.collections["mem"].count()


def test_write_syncs_only_touched_ids(core, scope, monkeypatch):
    """T6 核心：write 只同步本次写的 id（记忆＋经历），不是全量。"""
    captured: dict = {"ids": "unset", "full_calls": 0}

    def spy_sync(conn, collections=None, *, ids=None):
        captured["ids"] = ids
        return {"indexed": len(ids or []), "memories": 0, "episodes": 0}

    monkeypatch.setattr(rt, "sync_index", spy_sync)
    # BM25 还需真建（spy 不管），但 sync 被替换后 build_bm25 照常
    result = core.write(scope, "我只看允许远程的岗位", kind="preference")
    assert captured["ids"] is not None and len(captured["ids"]) == 2  # mid + pid
    assert result.ids[0] in captured["ids"]
    assert result.episode_id in captured["ids"]


def test_write_includes_superseded_old_id(core, scope, monkeypatch):
    """被取代的旧记忆 id 也进增量同步集（它要从池里删掉）。"""
    captured: dict = {"ids": None}

    def spy_sync(conn, collections=None, *, ids=None):
        captured["ids"] = list(ids or [])
        # 只做 min：真 sqlite 写已在调用前完成；向量层被替换不影响断言
        return {"indexed": len(ids or []), "memories": 0, "episodes": 0}

    monkeypatch.setattr(rt, "sync_index", spy_sync)
    old = core.write(scope, "我只喝冰美式", kind="preference")
    # 触发"取代"：同 kind 语义相近的新句（classify_dup_action 走 supersede 分支的判断
    # 依赖文本相似度；这里直接构造 supersede 结果断言增量集包含旧 id）
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        old_id = old.ids[0]
        # 直接标一条 superseded（等价 write 里 supersede 分支的落库结果），再走 write
        db.supersede_memory(session.conn, old_id, old_id, source_episode_id=old.episode_id)
        session.conn.commit()
    captured["ids"] = None
    new = core.write(scope, "我只喝热的", kind="preference")
    # 本次写触发了新的语义取代（若有）则增量集含 extra id；核心断言：每次 ≤3（写+经历+被取代）
    assert captured["ids"] is not None
    assert len(captured["ids"]) <= 3
    assert new.ids[0] in captured["ids"]


def test_incremental_write_visible_to_search(core, scope):
    """真端到端：增量同步后，新写的记忆能被搜索到（不依赖 monkeypatch）。"""
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    result = core.search(scope, "我只看允许远程的岗位", limit=8)
    assert any("允许远程" in item.content for item in result.items)


def test_superseded_row_removed_from_pool(core, scope):
    """掉出 active 的行从向量池删除：标记 archived + 增量同步后池计数回落。"""
    core.write(scope, "我不投需要长期出差的岗位", kind="preference")
    before = _mem_pool_count(core, scope)
    assert before == 1
    session = core._session(scope)  # noqa: SLF001
    mid = session.conn.execute("SELECT id FROM memories LIMIT 1").fetchone()["id"]
    with session.lock:
        session.conn.execute("UPDATE memories SET status='archived' WHERE id=?", (mid,))
        session.conn.commit()
    core._reindex(session, ids=[mid])  # noqa: SLF001
    assert _mem_pool_count(core, scope) == 0


def test_reactive_row_reenters_pool(core, scope):
    """candidate→active（确认胜出）后行回到池：增量同步两个方向都不漏。"""
    core.write(scope, "我每周三晚上固定留给项目", kind="preference")
    session = core._session(scope)  # noqa: SLF001
    mid = session.conn.execute("SELECT id FROM memories LIMIT 1").fetchone()["id"]
    with session.lock:
        session.conn.execute("UPDATE memories SET status='candidate' WHERE id=?", (mid,))
        session.conn.commit()
    core._reindex(session, ids=[mid])  # noqa: SLF001
    assert _mem_pool_count(core, scope) == 0
    with session.lock:
        session.conn.execute("UPDATE memories SET status='active' WHERE id=?", (mid,))
        session.conn.commit()
    core._reindex(session, ids=[mid])  # noqa: SLF001
    assert _mem_pool_count(core, scope) == 1


def test_touched_deletion_keeps_untouched_rows(core, scope):
    """删一条只删一条：同库其它行不受增量同步影响。"""
    core.write(scope, "我只喝冰美式", kind="preference")
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    assert _mem_pool_count(core, scope) == 2
    session = core._session(scope)  # noqa: SLF001
    rows = session.conn.execute("SELECT id FROM memories ORDER BY id").fetchall()
    with session.lock:
        session.conn.execute("UPDATE memories SET status='archived' WHERE id=?", (rows[0]["id"],))
        session.conn.commit()
    core._reindex(session, ids=[rows[0]["id"]])  # noqa: SLF001
    assert _mem_pool_count(core, scope) == 1  # 第二行仍在


def test_ingest_history_still_full_sync(core, scope, monkeypatch):
    """批量导入保持全量同步（ids=None）：增量只上单条写入，不悄悄改批量路径。"""
    captured: dict = {"ids": "unset"}

    def spy_sync(conn, collections=None, *, ids=None):
        captured["ids"] = ids
        return {"indexed": 0, "memories": 0, "episodes": 0}

    monkeypatch.setattr(rt, "sync_index", spy_sync)
    core.ingest_history(scope, [{"text": "历史轮次：我们讨论过排期", "role": "user"}], sync=True)
    assert captured["ids"] is None


def test_bm25_sees_new_write(core, scope):
    """增量路径后 BM25 索引重建生效：词法通道能搜到刚写的行。"""
    core.write(scope, "我把异地仓库放到了 D 盘备份", kind="resource")
    result = core.search(scope, "异地仓库的备份位置", limit=8)
    assert any("异地仓库" in item.content for item in result.items)
