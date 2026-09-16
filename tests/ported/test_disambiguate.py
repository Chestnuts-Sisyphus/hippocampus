# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.disambiguate.py 单元测试（P1 coverage 补测）。

实体消歧：check_same 判断两名字是否同一实体；merge_entities 合并。
注意：check_same 可能依赖 LLM——看实现决定是否 mock。
"""

import sqlite3

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import disambiguate


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.commit()
    yield c
    c.close()


def test_check_same_identical(monkeypatch):
    """LLM 判定路径：mock chat_json 返回 same=true → 返回 True。"""
    monkeypatch.setattr(
        "hippocampus.memory.disambiguate.chat_json",
        lambda *a, **k: {"same": True, "reason": "完全同名"},
    )
    same, reason = disambiguate.check_same("张三", "张三")
    assert same is True
    assert reason


def test_check_same_llm_failure(monkeypatch):
    """LLM 调用失败 → 返回 (False, '调用失败')，不抛异常。"""
    monkeypatch.setattr(
        "hippocampus.memory.disambiguate.chat_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("网络挂了")),
    )
    same, reason = disambiguate.check_same("A", "B")
    assert same is False
    assert "调用失败" in reason


def test_merge_entities_combines(conn):
    """合并后 keep 实体保留、drop 实体标记 merged、记忆转移。"""
    keep_id = db.add_entity(conn, "北京", "Concrete")
    drop_id = db.add_entity(conn, "北京市", "Concrete")
    db.add_memory(conn, "fact", "北京的天气", entity_ids=[drop_id])
    conn.commit()

    disambiguate.merge_entities(conn, keep_id, drop_id, "同义")
    conn.commit()

    # drop 实体标记 merged
    st = conn.execute("SELECT status FROM entities WHERE id=?", (drop_id,)).fetchone()[0]
    assert st == "merged"
    # keep 实体仍 active
    st = conn.execute("SELECT status FROM entities WHERE id=?", (keep_id,)).fetchone()[0]
    assert st == "active"
    # 记忆关系转移（至少 keep 实体有记忆）
    mems = db.get_memories_by_entity(conn, keep_id)
    assert len(mems) >= 1


def test_run_disambiguation_empty(conn):
    """空库跑消歧不炸。"""
    r = disambiguate.run_disambiguation(conn, max_pairs=10)
    assert isinstance(r, dict)


def test_run_disambiguation_merges_same(conn, monkeypatch):
    """mock LLM 判定 same=True → 合并发生。"""
    # 两个实体（先创建为主）
    e1 = db.add_entity(conn, "Hippocampus", "Abstract")
    e2 = db.add_entity(conn, "海马体", "Abstract")
    conn.commit()

    monkeypatch.setattr(
        "hippocampus.memory.disambiguate.check_same",
        lambda n1, n2: (True, "同义不同写法"),
    )
    r = disambiguate.run_disambiguation(conn, max_pairs=10)
    assert r["merged"] == 1
    # e2 被标记 merged
    st = conn.execute("SELECT status FROM entities WHERE id=?", (e2,)).fetchone()[0]
    assert st == "merged"
    # 别名并入 e1
    aliases = conn.execute("SELECT aliases FROM entities WHERE id=?", (e1,)).fetchone()[0]
    assert "海马体" in aliases


def test_run_disambiguation_no_merge_diff(conn, monkeypatch):
    """mock LLM 判定 same=False → 不合并。"""
    db.add_entity(conn, "存储结构", "Abstract")
    e2 = db.add_entity(conn, "检索策略", "Abstract")
    conn.commit()

    monkeypatch.setattr(
        "hippocampus.memory.disambiguate.check_same",
        lambda n1, n2: (False, "语义不同"),
    )
    r = disambiguate.run_disambiguation(conn, max_pairs=10)
    assert r["merged"] == 0
    st = conn.execute("SELECT status FROM entities WHERE id=?", (e2,)).fetchone()[0]
    assert st == "active"


def test_merge_entities_missing(conn):
    """实体不存在时 merge 不炸。"""
    disambiguate.merge_entities(conn, "nope1", "nope2", "test")
    # 无异常即通过
