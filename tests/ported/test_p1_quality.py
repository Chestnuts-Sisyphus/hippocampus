# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点1（P1 记忆质量）隔离单测。

瞬时状态过滤 + 生命周期降级 + 语义去重。
绝不碰真实库（database.DB_PATH 一律 monkeypatch 到 tmp_path），
语义通道用 monkeypatch 假数据，不依赖真实 chroma/embedding。

验收对应：瞬时状态样本（HEAD 哈希/「已完成 X」）不进库或定时失效用例过；
语义去重用例过（完全重复+语义重复两组）。
"""

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import dedup, extract, lifecycle, pipeline
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def tmp_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()


def _fake_extract(memories):
    """monkeypatch extract.chat_json 返回固定记忆列表（零 LLM）。"""

    def _fn(system, user, max_tokens):
        return {"entities": [], "memories": memories}

    return _fn


# ---------- 瞬时状态：prompt 约束（软约束，防 LLM 提取） ----------


def test_extract_prompt_has_transient_constraint():
    """提取 prompt 必须含「瞬时状态/任务描述不提取」约束（对应发现：573 条中
    259 条 status，含 HEAD 哈希 41 条 + 任务描述 44 条）。"""
    sys_a = extract.EXTRACT_SYSTEM
    assert "瞬时状态" in sys_a and "任务描述" in sys_a
    assert ("HEAD" in sys_a and "commit" in sys_a) or "哈希" in sys_a
    assert "已完成" in sys_a or "进度" in sys_a
    # 轨道B prompt 同步约束
    assert "瞬时状态" in extract.EXTRACT_RESPONSE_SYSTEM


# ---------- 瞬时状态：机械拦截（LLM 不听话时的兜底，硬规则） ----------


def test_transient_status_filtered_from_pipeline(tmp_conn, monkeypatch):
    """HEAD 哈希类 status 不入库；同批 fact 正常入库。"""
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "status", "content": "HEAD 在 1a2b3c4，正在重构检索", "entity_names": [], "source_quote": "HEAD 在 1a2b3c4"},
                {"type": "fact", "content": "Python 是解释型语言", "entity_names": [], "source_quote": "Python 是解释型语言"},
            ]
        ),
    )
    r = pipeline.process_user_message(tmp_conn, "sess_t", "HEAD 在 1a2b3c4，Python 是解释型语言")
    assert len(r["memory_ids"]) == 1
    row = tmp_conn.execute("SELECT * FROM memories WHERE id=?", (r["memory_ids"][0],)).fetchone()
    assert row["content"] == "Python 是解释型语言"


def test_transient_status_commit_run_patterns(tmp_conn, monkeypatch):
    """commit 哈希与 run 号类同样被拦截（模式全覆盖）。"""
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "status", "content": "commit abc1234 修了检索 bug", "entity_names": [], "source_quote": "commit abc1234"},
                {"type": "status", "content": "run 42 失败，需要重跑", "entity_names": [], "source_quote": "run 42"},
                {"type": "status", "content": "a1b2c3d4e5f60718293a4b5c6d7e8f901a2b3c4d 已合入", "entity_names": [], "source_quote": "哈希已合入"},
            ]
        ),
    )
    r = pipeline.process_user_message(tmp_conn, "sess_t", "commit abc1234 修了检索 bug，run 42 失败")
    assert r["memory_ids"] == []
    assert tmp_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_non_transient_status_kept(tmp_conn, monkeypatch):
    """非瞬时 status（bug 已修复，有长期意义的结果状态）不受拦截。"""
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "status", "content": "这个 bug 已经修复了", "entity_names": [], "source_quote": "这个 bug 已经修复了"},
            ]
        ),
    )
    r = pipeline.process_user_message(tmp_conn, "sess_t", "这个 bug 已经修复了")
    assert len(r["memory_ids"]) == 1


# ---------- 生命周期降级（历史瞬时状态定时失效） ----------


def test_degrade_transient_turns_stale_to_dormant(tmp_conn):
    """瞬时状态记忆超 7 天 → dormant；未超龄保持 active；非瞬时不受影响。"""
    eid = db.add_entity(tmp_conn, "项目", "Abstract")
    stale = db.add_memory(tmp_conn, "status", "run 42 失败，HEAD 在 1a2b3c4", entity_ids=[eid])
    fresh = db.add_memory(tmp_conn, "status", "run 43 失败，HEAD 在 deadbeef", entity_ids=[eid])
    normal = db.add_memory(tmp_conn, "status", "这个 bug 已经修复了", entity_ids=[eid])
    now = db.now_ms()
    tmp_conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 8 * 24 * 3600 * 1000, stale))
    tmp_conn.commit()

    n = lifecycle.degrade_transient(tmp_conn, now=now)
    assert n >= 1
    lc = {r["id"]: r["lifecycle"] for r in tmp_conn.execute("SELECT id, lifecycle FROM memories").fetchall()}
    assert lc[stale] == "dormant"
    assert lc[fresh] == "active"  # 未超龄不降级
    assert lc[normal] == "active"  # 非瞬时不降级
    # 不删除：dormant 仍可显式查询
    assert lifecycle.query_explicit(tmp_conn, stale) is not None


def test_scan_lifecycle_includes_transient_degrade(tmp_conn):
    """scan_lifecycle 集成降级（老库历史瞬时样本一次扫描即失效，to_dormant 含降级数）。"""
    eid = db.add_entity(tmp_conn, "项目", "Abstract")
    stale = db.add_memory(tmp_conn, "status", "HEAD 在 1a2b3c4", entity_ids=[eid])
    now = db.now_ms()
    tmp_conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 30 * 24 * 3600 * 1000, stale))
    tmp_conn.commit()
    r = lifecycle.scan_lifecycle(tmp_conn, verbose=False, now=now)
    assert r["to_dormant"] >= 1
    row = tmp_conn.execute("SELECT lifecycle FROM memories WHERE id=?", (stale,)).fetchone()
    assert row["lifecycle"] == "dormant"


# ---------- 打回补丁（08-16）：降级只限 type=status，与 is_transient_status 对齐 ----------


def test_degrade_transient_skips_fact_with_head(tmp_conn):
    """fact「HTTP HEAD 方法不带 body」超 7 天 → 仍 active（打回实测误伤场景）。"""
    eid = db.add_entity(tmp_conn, "HTTP", "Abstract")
    mid = db.add_memory(tmp_conn, "fact", "HTTP HEAD 方法不带 body", entity_ids=[eid])
    now = db.now_ms()
    tmp_conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 8 * 24 * 3600 * 1000, mid))
    tmp_conn.commit()
    n = lifecycle.degrade_transient(tmp_conn, now=now)
    assert n == 0
    row = tmp_conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "active"


def test_degrade_transient_skips_preference_with_task_desc(tmp_conn):
    """preference 含「任务描述」超 7 天 → 仍 active（非 status 不降级）。"""
    eid = db.add_entity(tmp_conn, "项目", "Abstract")
    mid = db.add_memory(tmp_conn, "preference", "任务描述要写清楚验收标准", entity_ids=[eid])
    now = db.now_ms()
    tmp_conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 8 * 24 * 3600 * 1000, mid))
    tmp_conn.commit()
    n = lifecycle.degrade_transient(tmp_conn, now=now)
    assert n == 0
    row = tmp_conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "active"


def test_degrade_transient_still_downgrades_status(tmp_conn):
    """status + 瞬时模式 + 超 7 天 → 仍降 dormant（打回补丁后 status 行为保持）。"""
    eid = db.add_entity(tmp_conn, "项目", "Abstract")
    mid = db.add_memory(tmp_conn, "status", "HEAD 在 1a2b3c4", entity_ids=[eid])
    now = db.now_ms()
    tmp_conn.execute("UPDATE memories SET created_at=? WHERE id=?", (now - 8 * 24 * 3600 * 1000, mid))
    tmp_conn.commit()
    n = lifecycle.degrade_transient(tmp_conn, now=now)
    assert n >= 1
    row = tmp_conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "dormant"


# ---------- 完全去重 ----------


def test_exact_duplicate_skipped(tmp_conn, monkeypatch):
    """完全重复：跨会话同内容第二次入库被跳过（第一组验收用例；
    P1 观察数据的重复主要来自跨会话导入）。"""
    mem = [{"type": "preference", "content": "我不喜欢打补丁式设计", "entity_names": [], "source_quote": "我不喜欢打补丁式设计"}]
    monkeypatch.setattr(extract, "chat_json", _fake_extract(mem))
    r1 = pipeline.process_user_message(tmp_conn, "sess_A", "我不喜欢打补丁式设计")
    assert len(r1["memory_ids"]) == 1
    r2 = pipeline.process_user_message(tmp_conn, "sess_B", "我不喜欢打补丁式设计", collections={})
    assert r2["memory_ids"] == []
    assert tmp_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_exact_duplicate_allowed_within_session(tmp_conn, monkeypatch):
    """同会话内重复（跨轮）放行——tracka 验收语义：同一会话提取各入库一次。"""
    mem = [{"type": "preference", "content": "我喜欢简洁回复", "entity_names": [], "source_quote": "喜欢简洁"}]
    monkeypatch.setattr(extract, "chat_json", _fake_extract(mem))
    r1 = pipeline.process_user_message(tmp_conn, "sess_same", "我喜欢简洁回复")
    r2 = pipeline.process_user_message(tmp_conn, "sess_same", "我喜欢简洁回复", collections={})
    assert len(r1["memory_ids"]) == 1
    assert len(r2["memory_ids"]) == 1
    assert tmp_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_exact_duplicate_normalized(tmp_conn):
    """完全重复含空白差异（规范化后相等）也命中。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    mid = db.add_memory(tmp_conn, "preference", "我不喜欢打补丁式设计", entity_ids=[eid])
    tmp_conn.commit()
    assert dedup.find_exact_duplicate(tmp_conn, "preference", "我不喜欢打补丁式设计。") == mid
    assert dedup.find_exact_duplicate(tmp_conn, "preference", " 我不喜欢打补丁式设计 ") == mid
    # 类型不同不命中（fact vs preference）
    assert dedup.find_exact_duplicate(tmp_conn, "fact", "我不喜欢打补丁式设计") is None
    # superseded 记忆不参与去重基准
    db.supersede_memory(tmp_conn, mid, mid)
    tmp_conn.commit()
    assert dedup.find_exact_duplicate(tmp_conn, "preference", "我不喜欢打补丁式设计") is None


# ---------- 语义去重（embedding 相似度，第二组验收用例） ----------


def _patch_semantic(monkeypatch, hits):
    monkeypatch.setattr(rt, "_query_embedding", lambda text: [0.0] * 384)
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: hits,
    )


def test_semantic_duplicate_skipped(tmp_conn, monkeypatch):
    """语义重复：跨会话相似度 ≥0.85 的同类型记忆 → 跳过（MiniLM 中文虚高 → 高阈值）。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    mid = db.add_memory(tmp_conn, "preference", "我不喜欢打补丁式设计，要浑然天成", entity_ids=[eid])
    tmp_conn.commit()
    _patch_semantic(monkeypatch, {mid: {"sim": 0.9, "kind": "memory"}})
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "preference", "content": "我讨厌打补丁式的设计", "entity_names": [], "source_quote": "我讨厌打补丁式的设计"},
            ]
        ),
    )
    r = pipeline.process_user_message(
        tmp_conn, "sess_B", "我讨厌打补丁式的设计", collections={"mem": object(), "ep": object()}
    )
    assert r["memory_ids"] == []
    assert tmp_conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_semantic_below_threshold_allowed(tmp_conn, monkeypatch):
    """相似度低于阈值（0.85 防御 MiniLM 虚高）→ 不入重复，正常入库。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    mid = db.add_memory(tmp_conn, "preference", "我不喜欢打补丁式设计，要浑然天成", entity_ids=[eid])
    tmp_conn.commit()
    _patch_semantic(monkeypatch, {mid: {"sim": 0.6, "kind": "memory"}})
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "preference", "content": "最近在关注分布式系统", "entity_names": [], "source_quote": "最近在关注分布式系统"},
            ]
        ),
    )
    r = pipeline.process_user_message(
        tmp_conn, "sess_t", "最近在关注分布式系统", collections={"mem": object(), "ep": object()}
    )
    assert len(r["memory_ids"]) == 1


def test_semantic_duplicate_type_scoped(tmp_conn, monkeypatch):
    """语义重复限同类型（preference 不因 fact 重复被跳）。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    mid = db.add_memory(tmp_conn, "fact", "我讨厌打补丁式设计", entity_ids=[eid])
    tmp_conn.commit()
    _patch_semantic(monkeypatch, {mid: {"sim": 0.9, "kind": "memory"}})
    monkeypatch.setattr(
        extract,
        "chat_json",
        _fake_extract(
            [
                {"type": "preference", "content": "我讨厌打补丁式设计", "entity_names": [], "source_quote": "我讨厌打补丁式设计"},
            ]
        ),
    )
    r = pipeline.process_user_message(
        tmp_conn, "sess_t", "我讨厌打补丁式设计", collections={"mem": object(), "ep": object()}
    )
    assert len(r["memory_ids"]) == 1  # 类型不同 → 不跳


def test_semantic_duplicate_excludes_shadow_and_superseded(tmp_conn, monkeypatch):
    """语义去重基准排除 shadow=1（轨道B 观察期）与 superseded 记忆。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    shadow_m = db.add_memory(tmp_conn, "preference", "我不喜欢打补丁式设计", entity_ids=[eid], shadow=1)
    dead_m = db.add_memory(tmp_conn, "preference", "我讨厌打补丁式设计", entity_ids=[eid])
    db.supersede_memory(tmp_conn, shadow_m, dead_m)
    tmp_conn.commit()
    _patch_semantic(
        monkeypatch,
        {shadow_m: {"sim": 0.95, "kind": "memory"}, dead_m: {"sim": 0.95, "kind": "memory"}},
    )
    assert dedup.find_semantic_duplicate(tmp_conn, {"mem": object()}, "preference", "我讨厌打补丁式设计") is None


def test_semantic_duplicate_soft_fail(tmp_conn, monkeypatch):
    """chroma/embedding 异常 → 软失败返回 None（不阻断入库）。"""
    eid = db.add_entity(tmp_conn, "设计", "Abstract")
    db.add_memory(tmp_conn, "preference", "我不喜欢打补丁式设计", entity_ids=[eid])
    tmp_conn.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("chroma 挂了")

    monkeypatch.setattr(rt, "_query_embedding", _boom)
    assert dedup.find_semantic_duplicate(tmp_conn, {"mem": object()}, "preference", "我讨厌打补丁式设计") is None
    # collections=None（导入路径）→ 不尝试语义
    assert dedup.find_semantic_duplicate(tmp_conn, None, "preference", "我讨厌打补丁式设计") is None
