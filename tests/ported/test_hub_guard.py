# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.hub_guard.py 单元测试（P1 coverage 补测）。

mega-hub 检测：实体关联记忆 ABOUT>30 条标记 is_hub。
用内存库隔离。
"""

import sqlite3

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import hub_guard


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.commit()
    yield c
    c.close()


def _entity_with_memories(conn, name, n):
    """造实体 + n 条关联记忆。"""
    eid = db.add_entity(conn, name, "Concrete")
    for i in range(n):
        db.add_memory(conn, "fact", f"{name} 的记忆{i}", entity_ids=[eid])
    conn.commit()
    return eid


def test_count_about(conn):
    eid = _entity_with_memories(conn, "小实体", 3)
    assert hub_guard.count_about(conn, eid) == 3
    assert hub_guard.count_about(conn, "nonexistent") == 0


def test_list_sub_entities(conn):
    eid = _entity_with_memories(conn, "有子实体", 2)
    sub = hub_guard.list_sub_entities(conn, eid)
    # 子实体 = 记忆里提取出的其他实体
    assert isinstance(sub, list)


def test_scan_hubs_flags_over_30(conn):
    _entity_with_memories(conn, "大枢纽", 35)
    _entity_with_memories(conn, "小实体", 5)
    r = hub_guard.scan_hubs(conn, verbose=False)
    # 返回 dict，应含 hub 相关统计
    assert isinstance(r, dict)
    # 35 条 > 30 → 应被标记 is_hub
    st = conn.execute("SELECT COUNT(*) FROM entities WHERE is_hub=1").fetchone()[0]
    assert st == 1


def test_scan_hubs_no_hub_under_30(conn):
    _entity_with_memories(conn, "普通实体", 10)
    hub_guard.scan_hubs(conn, verbose=False)
    st = conn.execute("SELECT COUNT(*) FROM entities WHERE is_hub=1").fetchone()[0]
    assert st == 0


def test_scan_hubs_idempotent(conn):
    """重复扫描不重复报错，is_hub 保持 1。"""
    _entity_with_memories(conn, "大枢纽2", 35)
    r1 = hub_guard.scan_hubs(conn, verbose=False)
    assert len(r1["hubs"]) == 1
    # 第二次扫描：同一实体仍在 hubs，is_hub 仍为 1
    r2 = hub_guard.scan_hubs(conn, verbose=False)
    assert len(r2["hubs"]) == 1
    st = conn.execute("SELECT COUNT(*) FROM entities WHERE is_hub=1").fetchone()[0]
    assert st == 1


def test_list_sub_entities_neighbors(conn):
    """子实体 = PART_OF/RELATED_TO 邻居（仅 active）。"""
    hub = db.add_entity(conn, "大主题", "Abstract")
    sub = db.add_entity(conn, "子主题", "Abstract")
    merged = db.add_entity(conn, "已合并实体", "Abstract")
    conn.execute("UPDATE entities SET status='merged' WHERE id=?", (merged,))
    db.add_relation(conn, "entity", sub, "entity", hub, "PART_OF")
    db.add_relation(conn, "entity", merged, "entity", hub, "RELATED_TO")
    conn.commit()

    subs = hub_guard.list_sub_entities(conn, hub)
    names = [s["name"] for s in subs]
    assert "子主题" in names
    assert "已合并实体" not in names  # merged 实体不列出
    assert hub_guard.list_sub_entities(conn, "nonexistent") == []
