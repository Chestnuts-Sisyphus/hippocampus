# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.retrieval_guard.py 单元测试（P1 coverage 补测）。

第二重漏提取检测（检索兜底）。LLM 依赖（missed_extract.extract_entities_only）
必须 mock——只测触发逻辑与补提取链路，不真调 LLM。
"""

import json
import sqlite3

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import retrieval_guard as rg


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.commit()
    yield c
    c.close()


def _seed_episode(conn, content="北京欢迎你", entity_ids=None):
    """entity_ids=None 时自动造一个实体关联；显式传 [] 则保持无实体。"""
    eid = None
    if entity_ids is None:
        eid = db.add_entity(conn, "北京", "Concrete")
        entity_ids = [eid]
    pid = db.add_episode(conn, "s1", "user", content, entity_ids=entity_ids)
    conn.commit()
    return pid, eid


def test_check_tracker_empty(conn):
    """无记录 → (0, '')。"""
    pid, _ = _seed_episode(conn)
    assert rg._check_tracker(conn, pid) == (0, "")


def test_run_guard_no_query_entity(conn):
    """查询无实体 → 不触发（返回空）。"""
    pid, _ = _seed_episode(conn)
    r = rg.run_retrieval_guard(conn, [], {"semantic_episodes": [{"doc_id": pid}]}, verbose=False)
    assert r == []


def test_backfill_with_entities(conn, monkeypatch):
    """mock 提取到实体 → 补 MENTIONS 关系 + tracker 标 fixed。"""
    pid, _ = _seed_episode(conn, "今天去了长城", entity_ids=[])
    # mock LLM 提取：返回一个实体
    monkeypatch.setattr(
        "hippocampus.memory.missed_extract.extract_entities_only",
        lambda content: [{"name": "长城", "type": "Concrete", "aliases": []}],
    )
    ok = rg._backfill_episode(conn, pid, verbose=False)
    assert ok is True
    # tracker 标 fixed
    assert rg._check_tracker(conn, pid)[1] == "fixed"
    # 经历补上了实体
    row = conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()
    assert len(json.loads(row["entity_ids"])) == 1


def test_backfill_no_entities_three_times(conn, monkeypatch):
    """3 次提取都空 → no_entity_confirmed 跳过。"""
    pid, _ = _seed_episode(conn, "无实体内容", entity_ids=[])
    monkeypatch.setattr("hippocampus.memory.missed_extract.extract_entities_only", lambda content: [])
    for _ in range(2):
        assert rg._backfill_episode(conn, pid, verbose=False) is False
    assert rg._check_tracker(conn, pid)[1] == "pending"
    # 第 3 次 → no_entity_confirmed
    assert rg._backfill_episode(conn, pid, verbose=False) is False
    assert rg._check_tracker(conn, pid)[1] == "no_entity_confirmed"


def test_backfill_missing_episode(conn):
    """经历不存在 → False，不炸。"""
    assert rg._backfill_episode(conn, "nonexistent", verbose=False) is False


def test_run_guard_full_chain(conn, monkeypatch):
    """完整触发链：语义命中但实体未命中 → 补提取并返回 id。"""
    # 经历无实体关联（漏提取场景）
    pid, _ = _seed_episode(conn, "今天去了长城", entity_ids=[])
    monkeypatch.setattr(
        "hippocampus.memory.missed_extract.extract_entities_only",
        lambda content: [{"name": "长城", "type": "Concrete", "aliases": []}],
    )
    # 查询实体 = 另一个实体（长城），语义通道命中该经历
    qeid = db.add_entity(conn, "长城", "Concrete")
    channels = {"semantic_episodes": [{"doc_id": pid}]}
    fixed = rg.run_retrieval_guard(conn, [qeid], channels, verbose=False)
    assert pid in fixed


def test_run_guard_channel_a_hit_skips(conn, monkeypatch):
    """通道A 已命中（经历含查询实体）→ 不算漏，跳过。"""
    pid, eid = _seed_episode(conn, "北京欢迎你")  # 经历关联北京实体
    channels = {"semantic_episodes": [{"doc_id": pid}]}
    fixed = rg.run_retrieval_guard(conn, [eid], channels, verbose=False)
    assert fixed == []


def test_run_guard_tracker_skip(conn, monkeypatch):
    """补提取 ≥3 次或 no_entity_confirmed → 跳过。"""
    from hippocampus.memory import missed_extract as me

    me._ensure_tracker(conn)
    pid, _ = _seed_episode(conn, "无实体内容", entity_ids=[])
    conn.execute(
        "INSERT OR REPLACE INTO missed_extract_tracker "
        "(episode_id, attempts, status, last_attempt_at, source) VALUES (?,?,?,?,?)",
        (pid, 3, "no_entity_confirmed", 0, "retroactive"),
    )
    conn.commit()
    qeid = db.add_entity(conn, "某实体", "Concrete")
    channels = {"semantic_episodes": [{"doc_id": pid}]}
    fixed = rg.run_retrieval_guard(conn, [qeid], channels, verbose=False)
    assert fixed == []
