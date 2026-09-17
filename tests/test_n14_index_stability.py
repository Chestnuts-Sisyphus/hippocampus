"""N14 守卫与索引稳定性（B4／B5／B6）：离线档守卫不空转 ＋ 向量索引读失败的自愈。

验收口径（缺口清单 N14）：
① 离线档守卫边界：模型不可用 → **规则抽取**补实体（不再只累计 attempts）；测试钉住；
② A30-3 间歇失败定位：失败原文＝语义通道读索引失败（Nothing found on disk）→ 语义与
   事件线索两条通道同时空掉 → 该题唯一可用的证据没了。本文件钉住"读失败会被修好"；
③ hnsw 根因处理：读失败的三级处置（重试 → **从 memory.db 重建向量池** → 再查）；
   实测：重试与"再 upsert 一次"都无效，删集合重建有效（见 docs/roadmap.md 的复现说明）。
"""

from __future__ import annotations

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import database as db
from hippocampus.memory import missed_extract
from hippocampus.memory import retrieval as rt

HNSW_ERROR = "Error executing plan: Internal error: Error creating hnsw segment reader: Nothing found on disk"


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


# ---------- ① 离线档守卫边界（B4） ----------


def test_offline_guard_uses_rule_extraction(core, scope):
    """离线档（测试环境默认无端点）守卫**真的补上实体**，而不是只累计 attempts。"""
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        pid = db.add_episode(session.conn, f"session_{scope.session}", "user", "我昨天在天津看了海河", entity_ids=[])
        session.conn.commit()

    handled = missed_extract.scan_and_fix(session.conn, verbose=False, max_episodes=5)
    assert handled >= 1, "孤立经历应被守卫处理"
    assert missed_extract.last_entity_source() == "rules", "离线档应走规则抽取"

    with session.lock:
        row = session.conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()
    assert row["entity_ids"] not in ("[]", "", None), "规则抽取应把实体补进经历"


def test_offline_guard_tracker_counts_attempts(core, scope):
    """规则抽取也提不出实体（纯寒暄）→ 仍按老口径累计 attempts，不无限重试。"""
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        pid = db.add_episode(session.conn, f"session_{scope.session}", "user", "嗯嗯", entity_ids=[])
        session.conn.commit()
    for _ in range(4):
        with session.lock:
            missed_extract.scan_and_fix(session.conn, verbose=False, max_episodes=5)
    with session.lock:
        tr = session.conn.execute(
            "SELECT attempts, status FROM missed_extract_tracker WHERE episode_id=?", (pid,)
        ).fetchone()
    assert tr is not None and tr["status"] == "no_entity_confirmed", "提不出实体应在 3 次后标记跳过"


# ---------- ②／③ 向量索引读失败的自愈（B6／B5） ----------


def _break_first_queries(monkeypatch, times: int) -> dict:
    """让前 `times` 次向量查询按 hnsw 段读失败报错（之后恢复正常）。"""
    real = rt._query_collection  # noqa: SLF001
    calls = {"n": 0}

    def fake(collection, query, n, query_embedding=None):
        calls["n"] += 1
        if calls["n"] <= times:
            raise RuntimeError(HNSW_ERROR)
        return real(collection, query, n, query_embedding)

    monkeypatch.setattr(rt, "_query_collection", fake)
    return calls


def test_read_failure_rebuilds_pool_from_source_of_truth(core, scope, monkeypatch):
    """检索路径读索引失败 → 触发"从 memory.db 重建向量池" → 本次查询仍拿到结果。"""
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户原话")
    session = core._session(scope)  # noqa: SLF001
    session.warmup_vector_index()  # 先热一次（把预热探测的调用与本次故障隔开）

    calls = _break_first_queries(monkeypatch, times=2)  # 原始查询 + 重试都失败 → 走重建
    repaired = {"n": 0}
    real_repair = session.repair_vector_pool

    def spy(key: str = "mem"):
        repaired["n"] += 1
        return real_repair(key)

    monkeypatch.setattr(session, "repair_vector_pool", spy)

    result = core.search(scope, "我的期望城市是哪里", limit=5)
    assert calls["n"] >= 3, "原始查询失败后应有重试与重建后的再查"
    assert repaired["n"] >= 1, "读索引失败必须触发重建（重试与再 upsert 实测无效）"
    assert any("杭州" in item.content for item in result.items), "重建后应能检索到源真相里的记忆"


def test_warmup_repairs_index_on_open(core, scope, monkeypatch):
    """会话打开就预热：探测即失败 → 当场重建，用户第一次检索就是好的（不再等到对话中途才炸）。"""
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户原话")
    core._sessions.pop(scope.account, None)  # noqa: SLF001 - 模拟"重新打开库"
    calls = _break_first_queries(monkeypatch, times=1)

    core._session(scope)  # noqa: SLF001 - 打开会话（预热探测 → 失败 → 重建）
    assert calls["n"] >= 2, "预热探测失败后应重建并再探一次"

    result = core.search(scope, "我的期望城市是哪里", limit=5)
    assert any("杭州" in item.content for item in result.items), "预热修好后第一次检索就该有结果"
    assert rt.take_index_error() == "", "预热里的故障已经处理过，不应留给后续检索"


def test_semantic_search_without_repair_still_soft_fails():
    """不给重建回调时（老调用方）：读失败照旧软失败返回空，不抛异常炸掉检索链。"""
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError(HNSW_ERROR)

    assert rt.semantic_search(_Boom(), "任意查询", n=5, query_embedding=[0.0] * 8) == {}


def test_manual_rebuild_command(core, scope):
    """运维入口：core.rebuild_index 把两个池都重建成功（CLI `hippocampus index rebuild` 用它）。"""
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户原话")
    out = core.rebuild_index(scope, pool="all")
    assert out == {"mem": True, "ep": True}
    health = core.index_health(scope)
    assert health["healthy"] is True
    assert health["collection_count"] == health["active_memories"]


def test_cli_index_rebuild_runs(home, capsys):
    """CLI 端到端：`hippocampus index rebuild` 可跑、退出码 0、并打印索引健康。"""
    from hippocampus.cli import main

    assert main(["--home", str(home), "--account", "ci", "seed"]) == 0
    rc = main(["--home", str(home), "--account", "ci", "index", "rebuild"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "已从 memory.db 重建" in out
    assert "索引健康" in out


def test_legacy_wal_purge_is_manual_only():
    """自动路径不再手工删 chroma 队列（改只翻 automatically_purge 开关）：函数还在但只在 CLI。"""
    import inspect

    from hippocampus.memory import memory_bridge as mb

    src = inspect.getsource(mb.MemorySession._drain_index_if_needed)  # noqa: SLF001
    assert "purge_embeddings_wal" not in src, "会话初始化不得再手工清 chroma 的 WAL 行"
    assert "enable_queue_autopurge" in src, "应改为只写配置（让 chroma 自己回收）"


def test_enable_queue_autopurge_writes_config(home, tmp_path):
    """`enable_queue_autopurge`：**只写配置不删数据**（队列行数不变）。"""
    core = MemoryCore(home=home)
    scope = Scope(account="test", session="s1", source="user")
    try:
        core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户原话")
        session = core._session(scope)  # noqa: SLF001
        before = rt.embeddings_queue_depth(session.chroma_dir)
        assert rt.enable_queue_autopurge(session.chroma_dir) is True
        after = rt.embeddings_queue_depth(session.chroma_dir)
        assert after == before, "只翻开关，不动数据"
    finally:
        core.close()
