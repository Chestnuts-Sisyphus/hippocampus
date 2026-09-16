# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点6 6a（A3-2 离线巩固）隔离单测。

- 超龄（>7 天）未蒸馏 user episode 能产出记忆
- 新 episode（7 天内）不处理
- 学习开关 off 时整函数空跑 0 条
- 不删除经历（episode 保留，derived_memory_ids 写回）
- 已蒸馏 episode 不重复处理
绝不碰真实库：database.DB_PATH 一律 tmp_path。
"""

import json

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import offline_consolidation as oc


@pytest.fixture()
def tmp_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()


def _add_episode(conn, content, days_ago, session="sess_t"):
    event_time = db.now_ms() - int(days_ago * 24 * 3600 * 1000)
    pid = db.add_episode(conn, session, "user", content, event_time=event_time)
    conn.commit()
    return pid


def test_old_episode_produces_memory(tmp_conn, monkeypatch):
    """超龄未蒸馏 episode → 产出记忆；derived_memory_ids 写回；episode 不删。"""

    monkeypatch.setattr(
        "hippocampus.memory.extract.chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "preference", "content": "我不喜欢打补丁式设计",
                          "entity_names": [], "source_quote": "不喜欢打补丁式设计"}],
        },
    )
    pid = _add_episode(tmp_conn, "我不喜欢打补丁式设计", days_ago=8)
    r = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r["episodes"] == 1
    assert r["memories"] == 1
    # episode 保留
    ep = tmp_conn.execute("SELECT * FROM episodes WHERE id=?", (pid,)).fetchone()
    assert ep is not None
    mids = json.loads(ep["derived_memory_ids"])
    assert len(mids) == 1
    # 记忆入库且 source 指向该 episode
    row = tmp_conn.execute("SELECT * FROM memories WHERE id=?", (mids[0],)).fetchone()
    assert row is not None and row["source_episode_id"] == pid
    # 第二次跑：已蒸馏不再处理
    r2 = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r2["episodes"] == 0 and r2["memories"] == 0


def test_new_episode_not_processed(tmp_conn, monkeypatch):
    """新 episode（今天）不处理（不超龄）。"""
    monkeypatch.setattr(
        "hippocampus.memory.extract.chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "fact", "content": "数据库用 SQLite",
                          "entity_names": [], "source_quote": "SQLite"}],
        },
    )
    _add_episode(tmp_conn, "数据库用 SQLite", days_ago=0)
    r = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r["episodes"] == 0 and r["memories"] == 0
    # 边界：恰好 7 天前（≤ cutoff 处理）
    _add_episode(tmp_conn, "数据库用 SQLite", days_ago=7)
    r2 = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r2["episodes"] == 1


def test_learning_switch_off_noop(tmp_conn, monkeypatch):
    """学习开关 off（learning_enabled=false）→ 整函数空跑 0 条。"""
    monkeypatch.setattr(
        "hippocampus.memory.extract.chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "fact", "content": "数据库用 SQLite",
                          "entity_names": [], "source_quote": "SQLite"}],
        },
    )
    _add_episode(tmp_conn, "数据库用 SQLite", days_ago=8)
    tmp_conn.execute(
        "UPDATE param_snapshots SET params=json_set(params, '$.learning_enabled', 0) WHERE is_active=1"
    )
    tmp_conn.commit()
    r = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r == {"episodes": 0, "memories": 0, "skipped": 0}
    # 且 episode 未被标记蒸馏（下次开学习仍可处理）
    row = tmp_conn.execute("SELECT derived_memory_ids FROM episodes").fetchone()
    assert row["derived_memory_ids"] in ("[]", "")


def test_episode_not_deleted(tmp_conn, monkeypatch):
    """巩固后 episode 原样保留（不删除，ADD-only 铁律）。"""
    monkeypatch.setattr(
        "hippocampus.memory.extract.chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "fact", "content": "数据库用 SQLite",
                          "entity_names": [], "source_quote": "SQLite"}],
        },
    )
    pid = _add_episode(tmp_conn, "数据库用 SQLite", days_ago=8)
    oc.run_offline_consolidation(tmp_conn, collections=None)
    ep = tmp_conn.execute("SELECT content, role, timestamp FROM episodes WHERE id=?", (pid,)).fetchone()
    assert ep["content"] == "数据库用 SQLite" and ep["role"] == "user"


def test_transient_status_episode_skipped_memory(tmp_conn, monkeypatch):
    """episode 内容提取出瞬时状态 → 机械拦截不入库（但 episode 仍标记已蒸馏，防重复尝试）。"""
    monkeypatch.setattr(
        "hippocampus.memory.extract.chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [{"type": "status", "content": "HEAD 在 1a2b3c4",
                          "entity_names": [], "source_quote": "HEAD"}],
        },
    )
    _add_episode(tmp_conn, "HEAD 在 1a2b3c4", days_ago=8)
    r = oc.run_offline_consolidation(tmp_conn, collections=None)
    assert r["episodes"] == 1
    assert r["memories"] == 0, "瞬时状态不入库"
    row = tmp_conn.execute("SELECT derived_memory_ids FROM episodes").fetchone()
    assert json.loads(row["derived_memory_ids"]) == []
