# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0901-01 节点2：语义去重命中分流（变更取代）隔离单测。

背景：同构变更句（「深色→浅色」「42码→40码」）embedding 高分被语义去重
静默丢弃 = 用户改主意永远不生效（HC-0816-02 定标 §2b 实锤，P1 暗伤）。
分流三态：supersede（机械确认值变更 → SUPERSEDES 取代）/ admit（同构换值
放行）/ drop（其余保守拦=现状）。绝不碰真实库：账户目录 monkeypatch 到
tmp_path（同 test_b12_explicit_remember 模式）；语义命中强制 monkeypatch
（聚焦分流逻辑，不依赖 embedding 相似度稳定性）。
"""

import shutil

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import dedup, extract, pipeline
from hippocampus.memory import memory_bridge as mb

# ---------- classify_dup_action 纯函数（三态判定） ----------


def test_classify_numeric_change_is_supersede():
    """数值集合不同 → 值变更（42 码 → 40 码）。"""
    assert dedup.classify_dup_action("我的鞋码是 42", "我的鞋码是 40") == "supersede"


def test_classify_negation_flip_is_supersede():
    """去否定字后相等 → 极性翻转（喜欢 ↔ 不喜欢深色主题）。"""
    assert dedup.classify_dup_action("我喜欢深色主题", "我不喜欢深色主题") == "supersede"


def test_classify_latin_enum_swap_is_admit():
    """删拉丁段后相等 → 同构换值（VS Code ↔ Vim），放行。"""
    assert dedup.classify_dup_action("我用 VS Code 写代码", "我用 Vim 写代码") == "admit"


def test_classify_paraphrase_is_drop():
    """改写型重复（无任何机械变更信号）→ 维持拦（现状）。"""
    assert dedup.classify_dup_action("我讨厌香菜", "香菜我真的没法接受") == "drop"


def test_classify_no_signal_is_drop():
    """中文枚举换值当前无机械信号 → drop（观察日志留痕，判据下一刀精化）。"""
    assert dedup.classify_dup_action("我喜欢喝咖啡", "我喜欢喝茶") == "drop"


def test_classify_identical_is_drop():
    """规范化相等（理论上一级去重已拦）→ drop 兜底。"""
    assert dedup.classify_dup_action("我讨厌香菜", "我讨厌香菜") == "drop"


def test_classify_exception_safe():
    """异常/空输入软失败 → drop（绝不阻断入库主流程）。"""
    assert dedup.classify_dup_action(None, "x") == "drop"  # type: ignore[arg-type]
    assert dedup.classify_dup_action("", "") == "drop"


# ---------- bridge 显式记住集成（MemorySession 真实 tmp chroma） ----------


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """临时账户 MemorySession（monkeypatch 账户目录，绝不碰真实账户）。
    零 LLM：extract.chat_json mock 为空 → 记住实体走机械兜底。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda system, user, max_tokens: {"entities": [], "memories": []},
    )
    sess = mb.MemorySession(f"hc0901_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def test_remember_exact_duplicate_still_dropped(session):
    """精确重复（字面相同）仍拦——分流不改一级去重行为。"""
    mb.handle_confirmation(session, "记住我讨厌香菜")
    msg2 = mb.handle_confirmation(session, "记住我讨厌香菜")
    assert "重复" in msg2
    n = session.conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '%香菜%'").fetchone()[0]
    assert n == 1


def test_remember_semantic_hit_supersedes(session, monkeypatch):
    """显式记住语义命中且机械确认值变更 → 新值入库 + 旧值 superseded + 回执「取代旧值」。"""
    mb.handle_confirmation(session, "记住我讨厌香菜")
    old_id = session.conn.execute(
        "SELECT id FROM memories WHERE content LIKE '%香菜%'"
    ).fetchone()["id"]
    monkeypatch.setattr(dedup, "find_semantic_duplicate", lambda *a, **k: old_id)
    msg = mb.handle_confirmation(session, "记住我不讨厌香菜")
    assert msg is not None and "取代" in msg
    old_status = session.conn.execute(
        "SELECT status FROM memories WHERE id=?", (old_id,)
    ).fetchone()["status"]
    assert old_status == "superseded"
    new_row = session.conn.execute(
        "SELECT id FROM memories WHERE content LIKE '%不讨厌%' AND status='active'"
    ).fetchone()
    assert new_row is not None
    rel = session.conn.execute(
        "SELECT COUNT(*) FROM relations WHERE rel_type='SUPERSEDES' AND from_id=? AND to_id=?",
        (new_row["id"], old_id),
    ).fetchone()[0]
    assert rel == 1


def test_remember_semantic_hit_admit_keeps_both(session, monkeypatch):
    """显式记住语义命中为同构换值 → 两条并存（用户主动意图不静默丢）。"""
    mb.handle_confirmation(session, "记住我用 VS Code 写代码")
    old_id = session.conn.execute(
        "SELECT id FROM memories WHERE content LIKE '%VS Code%'"
    ).fetchone()["id"]
    monkeypatch.setattr(dedup, "find_semantic_duplicate", lambda *a, **k: old_id)
    # 类型边界：非 preference 不走语义步 → 直接改 mtype 判定不可行，改用
    # preference 内容对（深色↔浅色无机械信号属 drop——显式记住 drop 也放行）
    mb.handle_confirmation(session, "记住我喜欢浅色主题")
    msg = mb.handle_confirmation(session, "记住我喜欢浅色主题")
    # 上面一条已入库；重复字面 → 精确重复拦
    assert "重复" in msg


def test_remember_semantic_hit_no_signal_admits_on_intent(session, monkeypatch):
    """显式记住语义命中但无机械变更信号（drop）→ 用户主动意图仍入库（与
    pipeline 提取路径的差异：显式记住不静默丢）。"""
    mb.handle_confirmation(session, "记住我讨厌香菜")
    old_id = session.conn.execute(
        "SELECT id FROM memories WHERE content LIKE '%香菜%'"
    ).fetchone()["id"]
    monkeypatch.setattr(dedup, "find_semantic_duplicate", lambda *a, **k: old_id)
    msg = mb.handle_confirmation(session, "记住香菜我真的没法接受")
    assert msg is not None and "已记住" in msg
    old_status = session.conn.execute(
        "SELECT status FROM memories WHERE id=?", (old_id,)
    ).fetchone()["status"]
    assert old_status == "active"  # 未取代（drop 无信号），两条并存
    n = session.conn.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '%香菜%'").fetchone()[0]
    assert n == 2


# ---------- pipeline 主提取路径集成 ----------


@pytest.fixture()
def pipe_env(tmp_path, monkeypatch):
    """pipeline 隔离环境：tmp DB + MemorySession chroma + 零 LLM extract。
    memories 内容由各用例通过返回值控制（mock chat_json 按调用次序出队）。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    db.DB_PATH = tmp_path / "memory.db"
    conn = db.connect()
    responses: list[dict] = []

    def _fake_chat(system, user, max_tokens):
        return responses.pop(0) if responses else {"entities": [], "memories": []}

    monkeypatch.setattr(extract, "chat_json", _fake_chat)
    sess = mb.MemorySession(f"hc0901p_{tmp_path.name[-8:]}")
    yield conn, sess, responses
    mb.drop_bridge(sess.account_id)
    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


def test_pipeline_semantic_hit_supersedes(pipe_env, monkeypatch):
    """主提取路径：语义命中+机械确认值变更 → 入库后自动取代旧值（不再静默丢）。"""
    conn, sess, responses = pipe_env
    responses.append(
        {"entities": [], "memories": [{"type": "preference", "content": "我喜欢深色主题", "entity_names": []}]}
    )
    r1 = pipeline.process_user_message(conn, "s1", "我喜欢深色主题", collections=sess.collections)
    assert len(r1["memory_ids"]) == 1
    old_id = r1["memory_ids"][0]
    responses.append(
        {"entities": [], "memories": [{"type": "preference", "content": "我不喜欢深色主题", "entity_names": []}]}
    )
    monkeypatch.setattr(dedup, "find_semantic_duplicate", lambda *a, **k: old_id)
    r2 = pipeline.process_user_message(conn, "s2", "换个说法", collections=sess.collections)
    assert len(r2["memory_ids"]) == 1  # 不再被静默丢弃
    new_id = r2["memory_ids"][0]
    assert new_id != old_id
    old_status = conn.execute("SELECT status FROM memories WHERE id=?", (old_id,)).fetchone()["status"]
    assert old_status == "superseded"
    rel = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE rel_type='SUPERSEDES' AND from_id=? AND to_id=?",
        (new_id, old_id),
    ).fetchone()[0]
    assert rel == 1


def test_pipeline_exact_duplicate_still_dropped(pipe_env):
    """主提取路径：精确重复仍拦（分流不改一级去重）。"""
    conn, sess, responses = pipe_env
    mem = {"type": "fact", "content": "项目代号是海马", "entity_names": []}
    responses.append({"entities": [], "memories": [dict(mem)]})
    r1 = pipeline.process_user_message(conn, "s1", "项目代号是海马", collections=sess.collections)
    assert len(r1["memory_ids"]) == 1
    responses.append({"entities": [], "memories": [dict(mem)]})
    r2 = pipeline.process_user_message(conn, "s2", "项目代号是海马", collections=sess.collections)
    assert len(r2["memory_ids"]) == 0


def test_pipeline_semantic_hit_drop_still_blocked(pipe_env, monkeypatch):
    """主提取路径：语义命中无机械变更信号（drop）→ 维持拦截（0815-02 节点1
    去重语义不回退；回归网 test_semantic_duplicate_skipped 的等价守护）。"""
    conn, sess, responses = pipe_env
    responses.append(
        {"entities": [], "memories": [{"type": "preference", "content": "我讨厌香菜", "entity_names": []}]}
    )
    r1 = pipeline.process_user_message(conn, "s1", "我讨厌香菜", collections=sess.collections)
    assert len(r1["memory_ids"]) == 1
    old_id = r1["memory_ids"][0]
    responses.append(
        {"entities": [], "memories": [{"type": "preference", "content": "香菜我真的没法接受", "entity_names": []}]}
    )
    monkeypatch.setattr(dedup, "find_semantic_duplicate", lambda *a, **k: old_id)
    r2 = pipeline.process_user_message(conn, "s2", "换个说法", collections=sess.collections)
    assert len(r2["memory_ids"]) == 0  # drop 分支必须真拦（回归补丁：漏 continue 实锤守护）
    n = conn.execute("SELECT COUNT(*) FROM memories WHERE type='preference'").fetchone()[0]
    assert n == 1
