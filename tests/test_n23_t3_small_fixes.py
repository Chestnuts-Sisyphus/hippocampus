"""T3 小修批量-B（缺口 A8/A9/A11/A12/A16）的验收测试。

- A9：hub_guard 标记**不改变检索行为**（检索侧不消费，按前身设计）；
- A11：`memory candidates` 收敛为唯一入口，`memory review` 默认视图打弃用提示；
- A12：`core.candidate_audit` 候选错分计数视图（占比/类型/滞留/未决块/抽样）；
- A8：shadow 列登记为观察轨内部实现（docs/naming.md 文本断言防回退）；
- A16：二轮清单 A30-3 状态清理为"已解决（N14）"（文本断言）。
"""

from __future__ import annotations

import pytest

from hippocampus.cli import main as cli_main
from hippocampus.core import Scope
from hippocampus.memory import database as db


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


# ---------- A9：hub 标记不消费 ----------


def test_hub_flag_does_not_filter_retrieval(home, scope):
    """entities.is_hub=1 之后检索结果不变（检索侧不据 is_hub 过滤；docs/naming.md 登记）。"""
    from hippocampus.core import MemoryCore

    core = MemoryCore(home=home)
    try:
        session = core._session(scope)  # noqa: SLF001
        eid = db.add_entity(session.conn, "大枢纽", "Concrete")
        with session.lock:
            core.write(scope, "大枢纽 的记忆 5 是每周三开会", kind="fact")
        row = session.conn.execute(
            "SELECT id FROM memories WHERE content LIKE '%记忆 5%' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        mid = row["id"]
        session.conn.execute(
            "INSERT INTO relations (from_type, from_id, to_type, to_id, rel_type, source_episode_id, extracted_at) "
            "VALUES ('memory', ?, 'entity', ?, 'ABOUT', '', ?)",
            (mid, eid, db.now_ms()),
        )
        session.conn.commit()

        inj_before = core.inject_finalize(scope, "大枢纽 的记忆 5 是每周三开会")
        ids_before = set(inj_before.injected_ids)
        assert mid in ids_before, "基线：该记忆应被检索到"

        # 手动打 hub 标记（等价 scan_hubs 的输出），检索行为必须不变
        session.conn.execute("UPDATE entities SET is_hub=1, updated_at=? WHERE id=?", (db.now_ms(), eid))
        session.conn.commit()
        inj_after = core.inject_finalize(scope, "大枢纽 的记忆 5 是每周三开会")
        assert set(inj_after.injected_ids) == ids_before
    finally:
        core.close()


# ---------- A12：candidate_audit 计数视图 ----------


def test_candidate_audit_counts_and_buckets(home, scope):
    """候选审计：占比/类型/滞留桶/未决块/抽样 id 齐全；纯计数不删改。"""
    from hippocampus.core import MemoryCore

    core = MemoryCore(home=home)
    try:
        core.write(scope, "我只看允许远程的岗位", kind="preference")
        core.write(scope, "我每周三晚上固定留给项目", kind="preference")
        session = core._session(scope)  # noqa: SLF001
        # 直接把其中一条降为 candidate（等价冲突挂起后的状态）
        with session.lock:
            rows = session.conn.execute(
                "SELECT id, type, created_at FROM memories ORDER BY created_at LIMIT 1"
            ).fetchone()
            session.conn.execute(
                "UPDATE memories SET status='candidate', updated_at=? WHERE id=?",
                (db.now_ms(), rows["id"]),
            )
            session.conn.commit()

        audit = core.candidate_audit(scope)
        assert audit["total_memories"] == 2
        assert audit["candidates"] == 1
        assert audit["share_pct"] == 50.0
        assert audit["by_kind"] == {"preference": 1}
        assert set(audit["by_age_days"]) == {"<1d", "1-7d", ">7d"}
        assert audit["by_age_days"]["<1d"] == 1
        assert audit["stale_candidates"] == 0
        assert audit["unresolved_blocks"] == 0
        assert audit["sample_ids"] == [rows["id"]]
    finally:
        core.close()


# ---------- A11：CLI 双入口收敛（保留别名 + 弃用提示） ----------


def test_memory_candidates_is_canonical(home, capsys):
    from hippocampus.core import MemoryCore, Scope

    core = MemoryCore(home=home)
    try:
        scope = Scope(account="default", session="cli", source="user")
        core.write(scope, "我只看允许远程的岗位", kind="preference")
        # 降为 candidate（等价冲突挂起状态），candidates 视图才看得到
        session = core._session(scope)  # noqa: SLF001
        session.conn.execute(
            "UPDATE memories SET status='candidate' WHERE status='active'"
        )
        session.conn.commit()
    finally:
        core.close()

    rc = cli_main(["--home", str(home), "memory", "candidates"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "等确认" in out
    assert "已弃用" not in err  # 正门不带弃用提示


def test_memory_review_default_deprecates_to_candidates(home, capsys):
    rc = cli_main(["--home", str(home), "memory", "review"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "已弃用" in err and "memory candidates" in err


def test_memory_candidates_audit_flag(home, capsys):
    from hippocampus.core import MemoryCore

    core = MemoryCore(home=home)
    try:
        core.write(Scope(account="default", session="cli", source="user"), "我只看允许远程的岗位", kind="preference")
    finally:
        core.close()

    rc = cli_main(["--home", str(home), "memory", "candidates", "--audit"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "候选审计" in out
    assert "占比" in out


# ---------- A8/A16：文档登记断言 ----------


def test_shadow_registered_as_observation_track_in_naming_docs():
    """A8：shadow 代码列登记为观察轨内部实现（docs/naming.md 四、术语与代码列对照）。"""
    from pathlib import Path

    text = (Path("docs/naming.md")).read_text(encoding="utf-8")
    assert "观察轨（模型侧）" in text
    assert "memories.shadow=1" in text
    assert "hub_guard" in text and "检索侧不消费" in text


def test_round2_gap_a30_3_marked_resolved():
    """A16：二轮缺口清单 A30-3 状态清理为已解决（N14）。"""
    from pathlib import Path

    text = Path("D:/AI/求职-天津秋招-202609/投递/Hippocampus-缺口总清单-二轮-20260917.md").read_text(encoding="utf-8")
    assert "已解决（N14）" in text
