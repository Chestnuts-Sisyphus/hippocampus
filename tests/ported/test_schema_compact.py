# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点6 6b（A3-3 图式化/去重增强）隔离单测。

- 三条语义相近的 preference → 1 条规则 + 旧条 superseded（不物理删除）
- 不相似的不合并
- collections 为 None → 空跑
- LLM 概括失败 → 跳过该组不合并
绝不碰真实库：database.DB_PATH 一律 tmp_path；语义/LLM 全 monkeypatch。
"""

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import llm
from hippocampus.memory import retrieval as rt
from hippocampus.memory import schema_compact as sc


@pytest.fixture()
def tmp_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()


def _add_pref(conn, content, entity="设计"):
    eid = db.add_entity(conn, entity, "Abstract")
    mid = db.add_memory(conn, "preference", content, entity_ids=[eid], source_quote=content[:40])
    conn.commit()
    return mid


def test_three_similar_prefs_compacted(tmp_conn, monkeypatch):
    """三条语义相近 preference → 1 条规则 + 旧条 superseded（不删）。"""
    m1 = _add_pref(tmp_conn, "我不喜欢打补丁式设计")
    m2 = _add_pref(tmp_conn, "我讨厌补丁式的做法")
    m3 = _add_pref(tmp_conn, "打补丁式设计不行")
    # 语义近邻：互相相似（≥0.85）
    monkeypatch.setattr(rt, "_query_embedding", lambda t: [0.0] * 384)

    def _fake_sem(collection, query, n=40, query_embedding=None):
        return {m1: {"sim": 0.9, "kind": "memory"}, m2: {"sim": 0.88, "kind": "memory"},
                m3: {"sim": 0.86, "kind": "memory"}}

    monkeypatch.setattr(rt, "semantic_search", _fake_sem)
    monkeypatch.setattr(llm, "chat_json", lambda system, user, max_tokens=300: {"rule": "用户反对打补丁式设计"})

    r = sc.compact_schemas(tmp_conn, collections={"mem": object()})
    assert r["rules"] == 1 and r["superseded"] == 3 and r["groups"] == 1
    # 旧条 superseded（不物理删除）
    for mid in (m1, m2, m3):
        row = tmp_conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "superseded"
    # 规则条入库
    winner = tmp_conn.execute(
        "SELECT * FROM memories WHERE scene_description LIKE '%图式化%'"
    ).fetchone()
    assert winner is not None and "反对打补丁" in winner["content"]
    # SUPERSEDES 关系已建
    rel = tmp_conn.execute(
        "SELECT COUNT(*) FROM relations WHERE from_id=? AND rel_type='SUPERSEDES'", (winner["id"],)
    ).fetchone()[0]
    assert rel == 3


def test_dissimilar_not_merged(tmp_conn, monkeypatch):
    """不相似的不合并（近邻 sim 低于阈值 → 无组）。"""
    m1 = _add_pref(tmp_conn, "我不喜欢打补丁式设计")
    m2 = _add_pref(tmp_conn, "我喜欢深色主题")
    monkeypatch.setattr(rt, "_query_embedding", lambda t: [0.0] * 384)
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {m2: {"sim": 0.4, "kind": "memory"}},
    )
    monkeypatch.setattr(llm, "chat_json", lambda system, user, max_tokens=300: {"rule": "不应发生"})
    r = sc.compact_schemas(tmp_conn, collections={"mem": object()})
    assert r["rules"] == 0 and r["superseded"] == 0
    for mid in (m1, m2):
        row = tmp_conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "active"


def test_collections_none_noop(tmp_conn):
    """collections 为 None → 空跑（无法语义聚组）。"""
    _add_pref(tmp_conn, "我不喜欢打补丁式设计")
    r = sc.compact_schemas(tmp_conn, collections=None)
    assert r == {"groups": 0, "rules": 0, "superseded": 0}


def test_llm_failure_skips_group(tmp_conn, monkeypatch):
    """LLM 概括失败 → 跳过该组（软失败，旧条保持 active）。"""
    m1 = _add_pref(tmp_conn, "我不喜欢打补丁式设计")
    m2 = _add_pref(tmp_conn, "我讨厌补丁式的做法")
    monkeypatch.setattr(rt, "_query_embedding", lambda t: [0.0] * 384)
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {m1: {"sim": 0.9, "kind": "memory"},
                                                               m2: {"sim": 0.88, "kind": "memory"}},
    )

    def _boom(system, user, max_tokens=300):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(llm, "chat_json", _boom)
    r = sc.compact_schemas(tmp_conn, collections={"mem": object()})
    assert r["rules"] == 0 and r["superseded"] == 0
    row = tmp_conn.execute("SELECT status FROM memories WHERE id=?", (m1,)).fetchone()
    assert row["status"] == "active"


def test_shadow_and_superseded_excluded(tmp_conn, monkeypatch):
    """候选排除 shadow=1（轨道B 观察期）与已 superseded 记忆。"""
    m1 = _add_pref(tmp_conn, "我不喜欢打补丁式设计")
    shadow_m = db.add_memory(tmp_conn, "preference", "我讨厌补丁式设计", shadow=1)
    tmp_conn.commit()
    monkeypatch.setattr(rt, "_query_embedding", lambda t: [0.0] * 384)
    # 近邻含 shadow 记忆 → 不并入组（不在候选）
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {shadow_m: {"sim": 0.9, "kind": "memory"}},
    )
    r = sc.compact_schemas(tmp_conn, collections={"mem": object()})
    assert r["rules"] == 0, "无同组候选（shadow 记忆不参与）"
    row = tmp_conn.execute("SELECT status FROM memories WHERE id=?", (m1,)).fetchone()
    assert row["status"] == "active"
