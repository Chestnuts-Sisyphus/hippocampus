# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点6 6d（A6-2 新鲜度）隔离单测。

注入排序在 updated_at 之外纳入 last_hit_at：最近用过的排在很久没用过的前面；
未命中过（last_hit_at=0）仍按 updated_at 兜底（现状语义不破）。
绝不碰真实库：database.DB_PATH 一律 tmp_path。
"""

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import memory_bridge as mb


@pytest.fixture()
def tmp_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()


def _add_fact(conn, content, entity="数据库"):
    eid = db.add_entity(conn, entity, "Abstract")
    mid = db.add_memory(conn, "fact", content, entity_ids=[eid], source_quote=content[:40])
    conn.commit()
    return mid


def test_touched_old_memory_ranks_first(tmp_conn):
    """两条同主题：只 touch 旧的那条（updated_at 更旧）→ 注入它排前。"""
    old_m = _add_fact(tmp_conn, "数据库用 PostgreSQL")
    new_m = _add_fact(tmp_conn, "数据库用 SQLite")
    # 新条 updated_at 更新（模拟后写入）
    tmp_conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (db.now_ms() + 1000, new_m))
    tmp_conn.commit()
    # 只 touch 旧条（模拟最近命中过）
    db.touch_memory(tmp_conn, old_m, at=db.now_ms())
    tmp_conn.commit()
    results = [
        {"doc_id": new_m, "kind": "memory", "channel": "semantic", "channel_rank": 0, "score": 0.9},
        {"doc_id": old_m, "kind": "memory", "channel": "semantic", "channel_rank": 1, "score": 0.8},
    ]
    out = mb._sort_injection_results(tmp_conn, results)
    assert out[0]["doc_id"] == old_m, "刚 touch 过的旧条必须排前（即使 updated_at 更旧）"
    assert out[1]["doc_id"] == new_m


def test_never_touched_falls_back_to_updated_at(tmp_conn):
    """两条都没 touch → 按 updated_at 降序（新在前，现状语义保持）。"""
    old_m = _add_fact(tmp_conn, "数据库用 PostgreSQL")
    new_m = _add_fact(tmp_conn, "数据库用 SQLite")
    tmp_conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (db.now_ms() + 1000, new_m))
    tmp_conn.commit()
    results = [
        {"doc_id": old_m, "kind": "memory", "channel": "semantic", "channel_rank": 0, "score": 0.8},
        {"doc_id": new_m, "kind": "memory", "channel": "semantic", "channel_rank": 1, "score": 0.9},
    ]
    out = mb._sort_injection_results(tmp_conn, results)
    assert out[0]["doc_id"] == new_m


def test_recently_touched_beats_older_touch(tmp_conn):
    """两条都 touch 过 → 最后命中时间新的排前。"""
    a = _add_fact(tmp_conn, "A 方案")
    b = _add_fact(tmp_conn, "B 方案")
    now = db.now_ms()
    db.touch_memory(tmp_conn, a, at=now - 3600_000)  # 1 小时前用过
    db.touch_memory(tmp_conn, b, at=now)  # 刚用过
    tmp_conn.commit()
    results = [
        {"doc_id": a, "kind": "memory", "channel": "semantic", "channel_rank": 0, "score": 0.9},
        {"doc_id": b, "kind": "memory", "channel": "semantic", "channel_rank": 1, "score": 0.8},
    ]
    out = mb._sort_injection_results(tmp_conn, results)
    assert out[0]["doc_id"] == b, "刚用过的应排前"


def test_stable_layer_uses_freshness(tmp_path, monkeypatch):
    """稳定层主题层排序同样纳入新鲜度（与流动层一致）。"""
    from hippocampus.memory import extract
    from hippocampus.memory import memory_bridge as mb

    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {"entities": [], "memories": []},
    )
    sess = mb.MemorySession(f"a62_{tmp_path.name[-8:]}")
    try:
        eid = db.add_entity(sess.conn, "数据库", "Abstract")
        old_m = db.add_memory(sess.conn, "preference", "数据库用 PostgreSQL", entity_ids=[eid])
        new_m = db.add_memory(sess.conn, "preference", "数据库用 SQLite", entity_ids=[eid])
        sess.conn.commit()
        sess.conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (db.now_ms() + 1000, new_m))
        sess.conn.commit()
        db.touch_memory(sess.conn, old_m, at=db.now_ms())
        sess.conn.commit()
        sess.reindex()
        monkeypatch.setattr(
            __import__("hippocampus.memory.retrieval", fromlist=["_"]), "semantic_search",
            lambda collection, query, n=40, query_embedding=None: {},
        )
        stable, fluid, filtered = mb.prepare_injection(sess, "数据库用什么？", flow="user")
        assert stable
        old_pos = stable.find("PostgreSQL")
        new_pos = stable.find("SQLite")
        assert old_pos != -1 and (new_pos == -1 or old_pos < new_pos), "touch 过的旧条在稳定层也应排前"
    finally:
        mb.drop_bridge(sess.account_id)
        import shutil

        shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)
