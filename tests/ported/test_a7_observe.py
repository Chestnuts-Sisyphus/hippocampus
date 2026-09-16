# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点6 6e（A7 观察落点 + §2c「已完成X」机械拦截收口）隔离单测。

- A7：prepare_injection 后账户 observe.jsonl 有 injection 记录（query + 注入 id）；
  确认块消费后有 confirmation 记录（decision/winner/loser）
- §2c「已完成 X」：status「部署已完成」类机械拦截不入库；「这个 bug 已经修复了」必须留下
绝不碰真实库：账户目录/DB_PATH 一律 tmp_path；语义/提取 mock。
"""

import json

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import extract, observe_log
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {"entities": [], "memories": []},
    )
    sess = mb.MemorySession(f"a7_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def _read_observe(session):
    path = observe_log.observe_path(session.account_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").strip().split("\n") if line]


def test_injection_logged(session, monkeypatch):
    """prepare_injection 后 observe.jsonl 有 injection 记录（query + 注入 id）。"""
    eid = db.add_entity(session.conn, "数据库", "Abstract")
    mid = db.add_memory(session.conn, "fact", "数据库用 SQLite", entity_ids=[eid])
    session.conn.commit()
    session.reindex()
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {mid: {"sim": 0.9, "kind": "memory"}},
    )
    mb.prepare_injection(session, "数据库用什么？", flow="user")
    records = _read_observe(session)
    assert records, "应有观察记录"
    inj = [r for r in records if r["event"] == "injection"]
    assert inj, "应有 injection 记录"
    assert inj[-1]["query"] == "数据库用什么？"
    assert mid in inj[-1]["injected_ids"], "注入记忆 id 应被记录"
    assert inj[-1]["ts"] > 0


def test_confirmation_logged(session, monkeypatch):
    """确认块消费后 observe.jsonl 有 confirmation 记录（decision/winner/loser）。"""
    eid = db.add_entity(session.conn, "主题", "Abstract")
    old_m = db.add_memory(session.conn, "status", "旧内容", entity_ids=[eid])
    new_m = db.add_memory(session.conn, "status", "新内容", entity_ids=[eid])
    session.conn.commit()
    from hippocampus.memory import confirm

    block = confirm.build_confirm_block(session.conn, [new_m], [(new_m, old_m, "状态翻转")])
    session.push_pending_block(block)
    msg = mb.handle_confirmation(session, "确认1")
    assert msg is not None and "已确认" in msg
    records = _read_observe(session)
    conf = [r for r in records if r["event"] == "confirmation"]
    assert conf, "应有 confirmation 记录"
    last = conf[-1]
    assert last["decision"] == "confirm"
    assert last["winner_id"] in (old_m, new_m)
    assert old_m in last["loser_ids"] or new_m in last["loser_ids"]


def test_injection_observation_soft_fail(session, monkeypatch):
    """观察记录异常不阻断注入（observe_path 抛错 → 注入仍正常）。"""
    eid = db.add_entity(session.conn, "数据库", "Abstract")
    mid = db.add_memory(session.conn, "fact", "数据库用 SQLite", entity_ids=[eid])
    session.conn.commit()
    session.reindex()
    monkeypatch.setattr(
        rt, "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {mid: {"sim": 0.9, "kind": "memory"}},
    )

    def _boom(aid):
        raise OSError("磁盘满了")

    monkeypatch.setattr(observe_log.account, "get_account_data_dir", _boom)
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable or fluid, "观察失败不得阻断注入"


# ---- §2c「已完成 X」机械拦截（正反例） ----


def test_completed_task_status_blocked(tmp_conn_factory, monkeypatch):
    """「部署已完成」「任务已交付」类完成态任务断言 status → 机械拦截不入库。"""
    conn = tmp_conn_factory
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [
                {"type": "status", "content": "部署已完成", "entity_names": [], "source_quote": "部署已完成"},
                {"type": "status", "content": "需求评审已交付", "entity_names": [], "source_quote": "已交付"},
            ],
        },
    )
    r = __import__("hippocampus.memory.pipeline", fromlist=["_"]).process_user_message(conn, "sess_t", "部署已完成，需求评审已交付")
    assert r["memory_ids"] == []
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_bug_fixed_status_kept(tmp_conn_factory, monkeypatch):
    """「这个 bug 已经修复了」必须留下（§2c 明确反例——不含完成态词）。"""
    conn = tmp_conn_factory
    monkeypatch.setattr(
        extract, "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [
                {"type": "status", "content": "这个 bug 已经修复了", "entity_names": [], "source_quote": "已经修复了"},
            ],
        },
    )
    r = __import__("hippocampus.memory.pipeline", fromlist=["_"]).process_user_message(conn, "sess_t", "这个 bug 已经修复了")
    assert len(r["memory_ids"]) == 1


@pytest.fixture()
def tmp_conn_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()
