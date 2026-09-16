# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点6 6c（A5-2 提取即重写 + §2c「记住挂实体」收口）隔离单测。

- 更新窗口：记忆被 touch 后，用户说同一主题的更新 → 新记忆与旧条冲突 →
  走既有确认机制 SUPERSEDES（不另造状态机）
- 「记住X」挂实体：记住香菜 → 香菜实体 + entity_ids 非空 → 查询含香菜走稳定层命中
绝不碰真实库：账户目录 monkeypatch 到 tmp_path；提取/冲突 mock。
"""

import pytest

from hippocampus.memory import conflict as conflict_mod
from hippocampus.memory import database as db
from hippocampus.memory import extract
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {"entities": [], "memories": []},
    )
    sess = mb.MemorySession(f"a52_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def _add_memory(session, content, mtype="fact", entity="数据库"):
    eid = db.add_entity(session.conn, entity, "Abstract")
    mid = db.add_memory(session.conn, mtype, content, entity_ids=[eid], source_quote=content[:40])
    session.conn.commit()
    return mid


def test_update_window_supersedes_old(session, monkeypatch):
    """A5-2 更新窗口：旧记忆被 touch（命中）→ 本轮提取同主题更新 → 冲突 →
    确认 → 旧条 superseded + SUPERSEDES 关系（走既有确认机制，不另造状态）。"""
    old_m = _add_memory(session, "数据库用 PostgreSQL", mtype="fact")
    session.reindex()
    # 旧记忆先被命中（touch）
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {old_m: {"sim": 0.9, "kind": "memory"}},
    )
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable or fluid
    row = session.conn.execute("SELECT last_hit_at FROM memories WHERE id=?", (old_m,)).fetchone()
    assert row["last_hit_at"] > 0, "旧记忆已被 touch"

    # 本轮用户更新：提取新 fact「数据库用 SQLite」+ 冲突检测判矛盾
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {
            "entities": [{"name": "数据库", "type": "Abstract", "aliases": []}],
            "memories": [{"type": "fact", "content": "数据库用 SQLite",
                          "entity_names": ["数据库"], "source_quote": "数据库用 SQLite"}],
        },
    )

    def _fake_conflicts(conn, new_mems):
        return {new_mems[0]["id"]: [(old_m, "技术栈矛盾")]}

    monkeypatch.setattr(conflict_mod, "batch_detect_conflicts", _fake_conflicts)
    block_text = mb.fire_track_a(session, "数据库用 SQLite 了")
    assert block_text, "应生成确认块"
    assert session.pending_blocks, "确认块已入队"

    # 用户确认「确认2」（编号=最近的记录 SQLite 胜出）→ 旧条 superseded
    msg = mb.handle_confirmation(session, "确认2")
    assert msg is not None and "已确认" in msg
    row = session.conn.execute("SELECT status FROM memories WHERE id=?", (old_m,)).fetchone()
    assert row["status"] == "superseded", "旧条被替代"
    winner = session.conn.execute(
        "SELECT id FROM memories WHERE content LIKE '%SQLite%' AND id != ?", (old_m,)
    ).fetchone()
    assert winner is not None
    rel = session.conn.execute(
        "SELECT COUNT(*) FROM relations WHERE from_id=? AND to_id=? AND rel_type='SUPERSEDES'",
        (winner["id"], old_m),
    ).fetchone()[0]
    assert rel == 1, "SUPERSEDES 关系已建（不另造状态）"


def test_remember_hangs_entity_and_stable_layer_hit(session, monkeypatch):
    """§2c 收口：记住我讨厌香菜 → 香菜实体 + entity_ids 非空；
    查询含香菜 → 稳定层（主题层机械实体匹配）能捞到该记忆。"""
    # 零 LLM：机械兜底 jieba 应建「香菜」实体
    msg = mb.handle_confirmation(session, "记住我讨厌香菜")
    assert msg is not None and "已记住" in msg
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%香菜%'").fetchone()
    assert row is not None
    eids = __import__("json").loads(row["entity_ids"])
    assert eids, "记住的记忆必须挂实体（§2c 收口）"
    ent = session.conn.execute("SELECT canonical_name FROM entities WHERE id=?", (eids[0],)).fetchone()
    assert ent["canonical_name"] == "香菜", f"实体应为香菜，实际 {ent['canonical_name']}"

    # 语义通道 mock 空 → 稳定层（主题层机械匹配）必须捞到
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {},
    )
    session.reindex()
    stable, fluid, filtered = mb.prepare_injection(session, "我讨厌香菜吗？", flow="user")
    assert "香菜" in stable, "稳定层应命中香菜记忆"


def test_fact_update_not_blocked_by_semantic_dedup(session, monkeypatch):
    """fact 同主题不同值（更新）不被语义去重拦截：语义相似 0.9 仍放行
    （dedup 语义层只对 preference；fact 更新走冲突检测/SUPERSEDES）。"""
    old_m = _add_memory(session, "数据库用 PostgreSQL", mtype="fact")
    session.conn.commit()
    # 语义近邻：新内容与旧 fact sim 0.9（MiniLM 中文虚高场景）
    monkeypatch.setattr(rt, "_query_embedding", lambda t: [0.0] * 384)
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {old_m: {"sim": 0.9, "kind": "memory"}},
    )
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "fact", "content": "数据库用 SQLite",
                          "entity_names": [], "source_quote": "数据库用 SQLite"}],
        },
    )
    r = __import__("hippocampus.memory.pipeline", fromlist=["_"]).process_user_message(
        session.conn, f"session_{session.account_id}", "数据库用 SQLite 了",
        memory_types=["preference", "fact"], collections=session.collections,
    )
    assert len(r["memory_ids"]) == 1, "fact 更新不得被语义去重拦截"
    assert session.conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '%SQLite%'").fetchone()[0] == 1
