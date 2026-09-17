"""N1 守卫接线（B1/B2/B3/B4）：MemoryCore 轮末与维护路径真的调用守卫模块。

验收口径（缺口清单 N1）：
- 构造"语义命中但实体未命中"的用例，断言 `consolidate` 轮末补实体生效（retrieval_guard）；
- 构造孤立经历（entity_ids 为空），断言轮末补实体（missed_extract）；
- 守卫全部失败路径软失败不阻断（consolidate 照常返回、写入照常生效）；
- hub_guard 已进维护扫描（maybe_run_maintenance 出 hubs 键，不炸）。
"""

from __future__ import annotations

import json

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import database as db
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def core(home):
    core = MemoryCore(home=home, vector=False)  # 词法档：不依赖 chromadb
    yield core
    core.close()


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def _session(core, scope):
    return core._session(scope)  # noqa: SLF001 - 测试直接取会话以构造库内状态


def test_retrieval_guard_backfill_on_consolidate(core, scope, monkeypatch):
    """语义命中但实体未命中 → 轮末补实体（retrieval_guard 经 consolidate 生效）。"""
    session = _session(core, scope)
    # 一条经历，实体关联为空（漏提取场景）；查询实体=长城
    pid = db.add_episode(session.conn, f"session_{scope.session}", "user", "今天去了长城", entity_ids=[])
    qeid = db.add_entity(session.conn, "长城", "Concrete")
    session.conn.commit()

    # mock 检索：返回"语义通道命中该经历"的 channels（触发条件二）
    def fake_retrieve(*_a, **_k):
        return {"results": [], "channels": {"entity_ids": [qeid], "semantic_episodes": [{"doc_id": pid, "sim": 0.9}]}}

    monkeypatch.setattr(rt, "retrieve", fake_retrieve)
    # mock LLM 提取（无端点环境）：补上"长城"实体
    monkeypatch.setattr(
        "hippocampus.memory.missed_extract.extract_entities_only",
        lambda content: [{"name": "长城", "type": "Concrete", "aliases": []}],
    )

    turn = core.consolidate(scope, user_text="今天去了长城")
    assert turn is not None
    row = session.conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()
    assert json.loads(row["entity_ids"]) == [qeid]  # 补实体生效
    # 只加不删：第二次检索仍命中时不再重复标记
    from hippocampus.memory import retrieval_guard as rg

    assert rg._check_tracker(session.conn, pid)[1] == "fixed"


def test_missed_extract_backfill_orphan_on_consolidate(core, scope, monkeypatch):
    """孤立经历（entity_ids 为空）→ 轮末补实体（missed_extract 经 consolidate 生效）。"""
    session = _session(core, scope)
    pid = db.add_episode(session.conn, f"session_{scope.session}", "user", "我最近在读《代码大全》", entity_ids=[])
    session.conn.commit()

    # mock 检索：无语义命中（触发不了 retrieval_guard），channels 空
    monkeypatch.setattr(rt, "retrieve", lambda *_a, **_k: {"results": [], "channels": {}})
    monkeypatch.setattr(
        "hippocampus.memory.missed_extract.extract_entities_only",
        lambda content: [{"name": "代码大全", "type": "Abstract", "aliases": []}],
    )

    core.consolidate(scope, user_text="我最近在读《代码大全》")
    row = session.conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()
    assert len(json.loads(row["entity_ids"])) == 1  # 孤立经历补上了实体


def test_guard_failure_does_not_block_consolidate(core, scope, monkeypatch):
    """守卫失败路径软失败：consolidate 照常返回、写入照常生效。"""
    session = _session(core, scope)

    def exploding_retrieve(*_a, **_k):
        raise RuntimeError("索引损坏（测试构造）")

    monkeypatch.setattr(rt, "retrieve", exploding_retrieve)
    # 再让 disambiguate / scan_and_fix 也可能炸（默认路径里它们都依赖 LLM，离线本就软失败）

    turn = core.consolidate(scope, user_text="我每周三晚上固定留给项目开发")
    assert turn is not None  # 不抛异常
    # 写入照常生效（固化没被守卫拖死）
    stats = core.stats(scope)
    assert stats["episodes"] >= 1


def test_hub_guard_runs_in_maintenance(core, scope, monkeypatch):
    """hub_guard 进维护扫描：ABOUT 超阈值实体被标记 is_hub。"""
    session = _session(core, scope)
    eid = db.add_entity(session.conn, "大枢纽", "Concrete")
    for i in range(31):
        db.add_memory(session.conn, "fact", f"大枢纽 的记忆{i}", entity_ids=[eid])
    session.conn.commit()
    # 强制清零节流，确保本次真的跑维护
    session._last_maintenance_scan_ms = 0  # noqa: SLF001
    out = session.maybe_run_maintenance(now=db.now_ms())
    assert "hubs" in out
    flag = session.conn.execute("SELECT is_hub FROM entities WHERE id=?", (eid,)).fetchone()[0]
    assert flag == 1
