# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点3（发现 32：在线生命周期空转）隔离单测。

发现 32：在线注入不更新 last_hit_at —— 命中记录仅在 cli_chat.py 更新，
proxy/memory_bridge 在线路径无写入 → 756 记忆 100% last_hit_at 空、
lifecycle 全 active（巩固/遗忘/复活从未触发）。

修复：memory_bridge 注入点补命中更新（稳定层 + 流动层都 touch）+ 注入路径
低频触发生命周期扫描（每天最多一次，scan_lifecycle 原无在线触发点）。

自验：在线注入后 last_hit_at 有值（隔离测试）；lifecycle 巩固/遗忘触发链路有值。
绝不碰真实库：账户目录 monkeypatch 到 tmp_path，语义通道 monkeypatch 假数据。
"""

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import lifecycle
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    sess = mb.MemorySession(f"p3_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def _add_memory(session, content, entity_name="主题", mtype="fact"):
    eid = db.add_entity(session.conn, entity_name, "Abstract")
    mid = db.add_memory(session.conn, mtype, content, entity_ids=[eid])
    session.conn.commit()
    return mid


def test_online_injection_updates_last_hit_at(session, monkeypatch):
    """发现 32 回归：在线注入（prepare_injection）后 last_hit_at 非空。
    记忆可能经稳定层（主题层机械命中）或流动层注入——无论哪层命中都必须 touch。"""
    mid = _add_memory(session, "数据库用 SQLite", "数据库")
    session.reindex()
    # 语义检索命中该记忆（sim 高于绝对底线 0.3）
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {mid: {"sim": 0.9, "kind": "memory"}},
    )
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable or fluid, "应有注入（稳定层或流动层）"
    row = session.conn.execute("SELECT last_hit_at FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["last_hit_at"] > 0, "在线注入后 last_hit_at 必须有值"


def test_stable_layer_hit_updates_last_hit_at(session, monkeypatch):
    """稳定层（主题层）命中也要 touch——原缺口：稳定层命中后流动层被剔除，
    touch 空转 → 全库 last_hit_at 空（发现 32 真因）。"""
    mid = _add_memory(session, "数据库用 SQLite", "数据库", mtype="preference")
    session.reindex()
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {},
    )
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable, "稳定层应注入偏好记忆"
    row = session.conn.execute("SELECT last_hit_at FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["last_hit_at"] > 0, "稳定层命中也必须更新 last_hit_at"


def test_lifecycle_scan_triggered_low_frequency(session, monkeypatch):
    """注入路径低频触发生命周期扫描（发现 32：scan_lifecycle 原无在线触发点，
    巩固/遗忘/复活从未生效）。首次注入触发，24h 内不再重复触发。"""
    mid = _add_memory(session, "数据库用 SQLite", "数据库")
    session.reindex()
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {mid: {"sim": 0.9, "kind": "memory"}},
    )
    calls = {"n": 0}

    def _fake_scan(conn, verbose=True, now=None):
        calls["n"] += 1
        return {"to_dormant": 0, "to_archived": 0, "total_active": 0}

    monkeypatch.setattr(lifecycle, "scan_lifecycle", _fake_scan)
    # 首次：节流戳为 0 → 触发
    session._last_lifecycle_scan_ms = 0
    mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert calls["n"] == 1
    assert session._last_lifecycle_scan_ms > 0
    # 间隔内（同一会话连续请求）不再触发
    mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert calls["n"] == 1


def test_lifecycle_rules_active_after_touch(session, monkeypatch):
    """touch 后生命周期规则生效：6 个月未命中 → dormant（巩固/遗忘链路有值）；
    dormant 被命中 → 复活 active（touch_memory 语义）。"""
    mid = _add_memory(session, "很久以前的经验", "旧主题")
    now = db.now_ms()
    session.conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 7 * 30 * 24 * 3600 * 1000, mid))
    session.conn.commit()
    # 未命中 → 6 个月超期 → dormant
    r = lifecycle.scan_lifecycle(session.conn, verbose=False, now=now)
    assert r["to_dormant"] >= 1
    row = session.conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "dormant"
    # 被在线命中 → 复活 active（touch_memory）
    db.touch_memory(session.conn, mid, at=now)
    session.conn.commit()
    row = session.conn.execute("SELECT lifecycle, last_hit_at FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "active"
    assert row["last_hit_at"] == now
