# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点5（B4-6 使用开关 + B4-7 学习开关）隔离单测。

- 使用开关：复用 injection_enabled（prepare_injection 既有闸，off 时注入空 + 重述警报不打扰）
- 学习开关：learning_enabled（B4-7 新增，off 时轨道A/轨道B/after_response 自动入库归零）
- 两开关默认 True、账户活跃 param_snapshots 持久化、重开会话仍在
- 口令：关闭记忆/打开记忆/停止学习/继续学习（整句匹配，经 handle_confirmation）；
  短句「关了」「不要了」不触发；任一开关 off 时「记住X/忘掉X」仍有效

绝不碰真实库：账户目录 monkeypatch 到 tmp_path；语义/提取/冲突全 monkeypatch。
"""

import pytest

from hippocampus.memory import conflict as conflict_mod
from hippocampus.memory import database as db
from hippocampus.memory import extract
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """临时账户 MemorySession（monkeypatch 账户目录，绝不碰真实账户）。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    sess = mb.MemorySession(f"b46_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def _fake_extract(memories):
    """monkeypatch extract.chat_json 返回固定记忆列表（零 LLM）。"""

    def _fn(system, user, max_tokens):
        return {"entities": [], "memories": memories}

    return _fn


def _seed_memory(session, content="数据库用 SQLite", mtype="fact", entity="数据库"):
    """库内预置一条正式记忆 + 同步索引（供注入检索命中）。"""
    eid = db.add_entity(session.conn, entity, "Abstract")
    mid = db.add_memory(session.conn, mtype, content, entity_ids=[eid])
    session.conn.commit()
    session.reindex()
    return mid


def _patch_semantic_hit(monkeypatch, mid):
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {mid: {"sim": 0.9, "kind": "memory"}},
    )


def _switch_value(session, key):
    row = session.conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    import json

    return json.loads(row["params"]).get(key)


# ── 1. 默认双开：注入非空 + 学习能入库 ──────────────────────────────────────


def test_default_switches_on_and_inject_works(session, monkeypatch):
    """默认：injection_enabled / learning_enabled 都 True；有记忆时注入非空。"""
    assert _switch_value(session, "injection_enabled") is True
    assert _switch_value(session, "learning_enabled") is True
    mid = _seed_memory(session)
    _patch_semantic_hit(monkeypatch, mid)
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable or fluid, "默认开时注入应非空"


def test_default_learning_works(session, monkeypatch):
    """默认开：轨道A 能从对话自动入库。"""
    monkeypatch.setattr(
        extract, "chat_json", _fake_extract([{"type": "preference", "content": "我喜欢简洁回复",
                                              "entity_names": [], "source_quote": "喜欢简洁"}])
    )
    monkeypatch.setattr(conflict_mod, "batch_detect_conflicts", lambda conn, new_mems: {})
    msg = mb.fire_track_a(session, "我喜欢简洁回复")
    assert msg == ""  # 无冲突不建确认块（正常）
    n = session.conn.execute("SELECT COUNT(*) FROM memories WHERE type='preference'").fetchone()[0]
    assert n == 1, "默认开时学习应能入库"


# ── 2. 使用开关：关闭记忆 → 注入空；打开记忆 → 恢复 ─────────────────────────


def test_injection_switch_off_then_on(session, monkeypatch):
    """「关闭记忆」→ prepare_injection 空；「打开记忆」→ 恢复注入。"""
    mid = _seed_memory(session)
    _patch_semantic_hit(monkeypatch, mid)

    assert "已关闭使用" in mb.handle_confirmation(session, "关闭记忆")
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable == "" and fluid == "" and filtered == []

    assert "已打开使用" in mb.handle_confirmation(session, "打开记忆")
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable or fluid, "打开记忆后注入应恢复"


def test_injection_switch_off_no_restatement_alarm(session, monkeypatch):
    """使用开关 off：重述警报也不再打扰（N3 在 injection_enabled 闸后，不触发诊断）。"""
    _seed_memory(session, "上次聊过的事", "fact", "旧事")
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {"ep_x": {"sim": 0.9, "kind": "episode"}},
    )
    mb.handle_confirmation(session, "关闭记忆")
    stable, fluid, filtered = mb.prepare_injection(session, "上次聊过的事", flow="user")
    assert stable == "" and fluid == ""
    assert session.take_alarm("上次聊过的事") == "", "使用开关 off 时不应产生重述警报"


# ── 3. 学习开关：停止学习 → 三条自动路径归零；继续学习 → 恢复 ───────────────


def _patch_learning_inputs(monkeypatch):
    monkeypatch.setattr(
        extract, "chat_json", _fake_extract([{"type": "preference", "content": "我喜欢简洁回复",
                                              "entity_names": [], "source_quote": "喜欢简洁"}])
    )
    monkeypatch.setattr(conflict_mod, "batch_detect_conflicts", lambda conn, new_mems: {})


def test_learning_switch_off_blocks_all_paths(session, monkeypatch):
    """「停止学习」→ fire_track_a / after_response / extract_response 自动入库 0 条。"""
    _patch_learning_inputs(monkeypatch)
    monkeypatch.setattr(
        mb, "_get_extract_response_fn",
        lambda: (lambda t: {"entities": [], "memories": [
            {"type": "resource", "content": "文档在 docs/README.md", "entity_names": [], "source_quote": "README"}
        ]}),
    )
    assert "已停止学习" in mb.handle_confirmation(session, "停止学习")

    assert mb.fire_track_a(session, "我喜欢简洁回复") == ""
    assert mb.after_response(session, "我喜欢简洁回复", []) == ""
    assert mb.extract_response(session, "AI 回复：文档在 docs/README.md") == []
    n = session.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert n == 0, "学习开关 off 时三条自动路径入库必须为 0"

    # 继续学习 → 恢复（轨道A 重新入库）
    assert "已继续学习" in mb.handle_confirmation(session, "继续学习")
    mb.fire_track_a(session, "我喜欢简洁回复")
    n = session.conn.execute("SELECT COUNT(*) FROM memories WHERE type='preference'").fetchone()[0]
    assert n == 1, "继续学习后自动入库应恢复"


def test_learning_switch_off_no_llm_call(session, monkeypatch):
    """学习开关 off：轨道B 不调 LLM 提取（提取函数不被调用）。"""
    calls = {"n": 0}

    def _fn(t):
        calls["n"] += 1
        return {"entities": [], "memories": []}

    monkeypatch.setattr(mb, "_get_extract_response_fn", lambda: _fn)
    mb.handle_confirmation(session, "停止学习")
    assert mb.extract_response(session, "AI 回复内容") == []
    assert calls["n"] == 0, "学习开关 off 时不应调用 LLM 提取"


# ── 4. 双关时「记住/忘掉」仍有效；「忘掉了」仍不是指令 ───────────────────────


def test_explicit_remember_works_when_both_off(session, monkeypatch):
    """两个开关都 off：用户亲口「记住X」仍入库并确认（≠ 自动学习 ≠ AI 引用）。"""
    mid = _seed_memory(session)
    _patch_semantic_hit(monkeypatch, mid)
    mb.handle_confirmation(session, "关闭记忆")
    mb.handle_confirmation(session, "停止学习")
    msg = mb.handle_confirmation(session, "记住我讨厌香菜")
    assert msg is not None and "已记住" in msg
    row = session.conn.execute("SELECT * FROM memories WHERE content LIKE '%香菜%'").fetchone()
    assert row is not None and row["status"] == "active"

    # 「忘掉了」仍不是指令（语气词坑防护保持）
    before = session.conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    assert mb.handle_confirmation(session, "忘掉了") is None
    after = session.conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    assert after == before


# ── 5. 持久化：关掉后重开 MemorySession（同一账户目录）开关还在 ──────────────


def test_switch_persists_across_session_reopen(tmp_path, monkeypatch):
    """关闭后重建 MemorySession（同一账户目录）→ 开关状态仍在（活跃快照持久化）。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    acct = f"b46p_{tmp_path.name[-8:]}"
    s1 = mb.MemorySession(acct)
    assert "已关闭使用" in mb.handle_confirmation(s1, "关闭记忆")
    assert "已停止学习" in mb.handle_confirmation(s1, "停止学习")
    assert _switch_value(s1, "injection_enabled") is False
    assert _switch_value(s1, "learning_enabled") is False
    mb.drop_bridge(acct)

    s2 = mb.MemorySession(acct)  # 重开会话（同一账户目录）
    try:
        assert _switch_value(s2, "injection_enabled") is False, "重开会话使用开关应保持 off"
        assert _switch_value(s2, "learning_enabled") is False, "重开会话学习开关应保持 off"
    finally:
        mb.drop_bridge(acct)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{acct}"), ignore_errors=True)


# ── 6. 短句「关了」「不要了」不改变开关 ──────────────────────────────────────


def test_short_phrases_do_not_toggle(session, monkeypatch):
    """短句「关了」「不要了」不是口令：开关值不变，走正常对话。"""
    assert _switch_value(session, "injection_enabled") is True
    assert _switch_value(session, "learning_enabled") is True
    assert mb.handle_confirmation(session, "关了") is None
    assert mb.handle_confirmation(session, "不要了") is None
    assert _switch_value(session, "injection_enabled") is True
    assert _switch_value(session, "learning_enabled") is True
