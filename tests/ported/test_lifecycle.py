# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.lifecycle.py 单元测试（P1 coverage 补测）。

容量分级三条规则：6 个月未命中→dormant；active>5000→最久先转；
dormant 2 年→archived。全部用临时内存库，不碰真实数据。
"""

import sqlite3

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import lifecycle


@pytest.fixture()
def conn():
    """内存库 + 完整 schema。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.commit()
    yield c
    c.close()


def _seed(conn, n=3, days_old=0, lifecycle_state="active"):
    """造 n 条记忆，created_at 按 days_old 天前。"""
    ids = []
    eid = db.add_entity(conn, "测试实体", "Concrete")
    now = db.now_ms()
    for i in range(n):
        mid = db.add_memory(conn, "fact", f"测试记忆{i}", entity_ids=[eid])
        conn.execute(
            "UPDATE memories SET created_at=?, last_hit_at=?, lifecycle=? WHERE id=?",
            (now - days_old * 86400_000, now - days_old * 86400_000, lifecycle_state, mid),
        )
        ids.append(mid)
    conn.commit()
    return ids


def test_rule1_old_active_to_dormant(conn):
    """规则1：active 超 6 个月未命中 → dormant。"""
    now = db.now_ms()
    _seed(conn, 1, days_old=200)  # ~6.6 个月
    r = lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    assert r["to_dormant"] == 1
    st = conn.execute("SELECT lifecycle FROM memories").fetchone()["lifecycle"]
    assert st == "dormant"


def test_rule1_fresh_stays_active(conn):
    """规则1 反例：最近命中的 active 不动。"""
    now = db.now_ms()
    _seed(conn, 1, days_old=1)  # 1 天前命中
    r = lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    assert r["to_dormant"] == 0
    st = conn.execute("SELECT lifecycle FROM memories").fetchone()["lifecycle"]
    assert st == "active"


def test_rule2_over_capacity(conn):
    """规则2：active 总量超 5000 → 最久未命中的先转 dormant。"""
    now = db.now_ms()
    # 造 5003 条（超 3 条）
    ids = []
    eid = db.add_entity(conn, "容量实体", "Concrete")
    for i in range(5003):
        mid = db.add_memory(conn, "fact", f"容量记忆{i}", entity_ids=[eid])
        conn.execute(
            "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
            (now - (i + 1) * 1000, now - (i + 1) * 1000, mid),
        )
        ids.append(mid)
    conn.commit()
    r = lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    assert r["to_dormant"] == 3
    assert r["total_active"] <= 5000


def test_rule3_dormant_to_archived(conn):
    """规则3：dormant 持续 2 年以上 → archived。"""
    now = db.now_ms()
    _seed(conn, 1, days_old=800, lifecycle_state="dormant")  # ~2.2 年
    r = lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    assert r["to_archived"] == 1
    st = conn.execute("SELECT lifecycle FROM memories").fetchone()["lifecycle"]
    assert st == "archived"


def test_rule3_recent_dormant_kept(conn):
    """规则3 反例：dormant 未满 2 年不动。"""
    now = db.now_ms()
    _seed(conn, 1, days_old=100, lifecycle_state="dormant")
    r = lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    assert r["to_archived"] == 0
    st = conn.execute("SELECT lifecycle FROM memories").fetchone()["lifecycle"]
    assert st == "dormant"


def test_query_explicit_finds_archived(conn):
    """archived 记忆显式查询仍可查到。"""
    mid = _seed(conn, 1)[0]
    row = lifecycle.query_explicit(conn, mid)
    assert row is not None
    assert row["id"] == mid
    assert lifecycle.query_explicit(conn, "nonexistent") is None


def test_list_all_lifecycles(conn):
    """统计分布。"""
    now = db.now_ms()
    _seed(conn, 2, days_old=1, lifecycle_state="active")
    _seed(conn, 1, days_old=800, lifecycle_state="dormant")
    lifecycle.scan_lifecycle(conn, verbose=False, now=now)
    dist = lifecycle.list_all_lifecycles(conn)
    assert dist.get("active") == 2
    assert dist.get("archived") == 1
