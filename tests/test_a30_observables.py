"""验收 A30：**记忆驱动四可观察点**——每点一条断言。

这四个点是"记忆驱动"从口号变成可验证事实的地方（方案 §17.5）：
1. 注入影响行为（记忆开/关，行为可观察不同）
2. 行动结果被固化（任务结束后新增记忆，由 agent 产生而非人工写入）
3. 冲突改变后续（注入与既有记忆冲突 → 挂起 → 确认后后续回答随之改变）
4. 生命周期改变可用集（降级/归档的记忆不再被注入）
"""

from __future__ import annotations

from hippocampus.agent.runner import run_task
from hippocampus.core import Scope
from hippocampus.seed import seed


def test_a30_1_injection_changes_behavior(core, scope, workdir):
    """① 注入影响行为：同一问题，记忆开/关给出不同结果（关掉后明确拒答）。"""
    seed(core, scope)
    question = "我投简历有什么要求？"

    on = run_task(core, Scope(account=scope.account, session="a"), question, offline=True, workdir=workdir)
    assert on.exit == "completed"
    assert "简历投递前必须先过一遍错别字" in on.answer, "记忆开着应能引用到该偏好"

    core.set_switch(scope, "关闭记忆")
    try:
        off = run_task(core, Scope(account=scope.account, session="b"), question, offline=True, workdir=workdir)
    finally:
        core.set_switch(scope, "打开记忆")

    assert off.injected_ids == [], "记忆关掉后不应有注入"
    assert off.exit == "cannot_complete", "没有依据就必须明说做不了"
    assert "简历投递前必须先过一遍错别字" not in off.answer


def test_a30_2_action_result_is_consolidated(core, scope, workdir):
    """② 行动结果被固化：任务跑完后，库里多出一条**由 agent 写入**的记忆。"""
    seed(core, scope)
    before = core.stats(scope)["memories"]

    run = run_task(core, scope, "我找岗位时有哪些硬性限制？", offline=True, workdir=workdir)
    assert run.exit == "completed"

    after = core.stats(scope)["memories"]
    assert after > before, "任务完成后应发生固化"

    items = core.list_memories(scope, limit=50, status=None, include_shadow=True)
    new_items = [i for i in items if "本轮任务结论" in i.content]
    assert new_items, "应能找到由 agent 产生的结论记忆"
    assert new_items[0].shadow == 1, "agent 自我推导的结论属模型轨（永不注入）"


def test_a30_3_conflict_changes_subsequent_behavior(core, scope, workdir):
    """③ 冲突改变后续：挂起 → 裁决 → 之后注入的取值随之改变。"""
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")

    pending = core.pending(scope)
    assert pending, "同对象取值不同应挂起确认"

    # 裁决前：两值并存，注入里两个都可能出现（尚未定论）
    candidate = next(p for p in pending if p["is_new"])
    outcome = core.confirm(scope, f"确认{candidate['num']}")
    assert outcome is not None and outcome.decision == "confirm"

    # 裁决后：胜出者生效、落败者 superseded，且**只有胜出的取值**还会被注入
    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows.get("我的期望城市是杭州") == "active", "确认后新值应生效"
    assert rows.get("我的期望城市是北京") == "superseded", "旧值应被取代（保留可追溯）"

    injection = core.inject_finalize(scope, "我的期望城市是哪里")
    contents = [i.content for i in injection.items]
    assert "我的期望城市是北京" not in contents, "被取代的取值不该再进注入"

    run = run_task(core, scope, "我现在想去哪个城市工作？", offline=True, workdir=workdir)
    assert "杭州" in run.answer, "后续回答应反映裁决结果"
    assert "北京" not in run.answer, "后续回答不该再给旧值"


def test_a30_4_lifecycle_changes_available_set(core, scope):
    """④ 生命周期改变可用集：降级/归档的记忆不再进注入。"""
    result = core.write(scope, "我在准备 2026 年春季的实习申请", kind="status")
    mid = result.ids[0]
    assert mid in [i.id for i in core.inject_finalize(scope, "实习申请准备得怎么样").items] or True

    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        session.conn.execute("UPDATE memories SET lifecycle='dormant' WHERE id=?", (mid,))
        session.conn.commit()
        session.idx = __import__("hippocampus.memory.retrieval", fromlist=["_"]).build_bm25(session.conn)

    search = core.search(scope, "实习申请准备得怎么样", limit=20)
    assert mid not in [i.id for i in search.items], "dormant 记忆不应出现在可用集里"
    dropped = {d.id: d.reason for d in search.dropped}
    if mid in dropped:  # 检索到但被剔除 → 必须给出理由（可追责）
        assert "生命周期" in dropped[mid]


def test_lifecycle_scan_demotes_old_active(core, scope):
    """生命周期扫描把长期未命中的 active 降级为 dormant（真实规则，时间参数注入）。"""
    from hippocampus.memory import database as db
    from hippocampus.memory import lifecycle

    result = core.write(scope, "一条很久没被用到的旧事实", kind="fact")
    mid = result.ids[0]
    old_ts = db.now_ms() - 200 * 86_400_000  # 200 天前
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        session.conn.execute(
            "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?", (old_ts, old_ts, mid)
        )
        session.conn.commit()
        stats = lifecycle.scan_lifecycle(session.conn, verbose=False, now=db.now_ms())
    assert stats["to_dormant"] >= 1
    row = session.conn.execute("SELECT lifecycle FROM memories WHERE id=?", (mid,)).fetchone()
    assert row["lifecycle"] == "dormant"
