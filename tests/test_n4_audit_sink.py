"""N4 AuditSink＋explain 完整形态（D1/D2）。

验收口径（缺口清单 N4）：
- 旁路 `AuditSink`：JSONL，top-N=50 上限，超限只记计数；
- `explain` 能答"那条为什么没进"（含**超 top-k 的候选**）；
- `observe` 与轨迹合并成一份 run 视图；
- 开关前后耗时基准进验收（审计开销有界）。
"""

from __future__ import annotations

import json
import time

import pytest

from hippocampus.core import Scope
from hippocampus.memory import audit as audit_mod
from hippocampus.memory import database as db


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")



def _toggle_audit(core, scope, enabled: bool) -> None:
    """开关审计（写活跃快照的 audit_enabled）。"""
    session = core._session(scope)  # noqa: SLF001
    row = session.conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    params = json.loads(row["params"])
    params["audit_enabled"] = enabled
    session.conn.execute(
        "UPDATE param_snapshots SET params=? WHERE id=?", (json.dumps(params, ensure_ascii=False), row["id"])
    )
    session.conn.commit()


def test_audit_cap_at_50(scope, tmp_path, monkeypatch):
    """top-N=50 上限：超出的候选只计计数，不落盘。"""
    monkeypatch.setenv("HIPPOCAMPUS_HOME", str(tmp_path))
    cands = [{"doc_id": f"m{i}", "kind": "memory", "channel": "semantic", "score": 1.0, "injected": i == 0,
              "dropped": False, "reason": ""} for i in range(60)]
    audit_mod.record_retrieval("audittest", query="q", candidates=cands, injected_ids=["m0"])
    events = audit_mod.load_events(audit_mod.audit_path("audittest"))
    assert len(events) == 1
    ev = events[0]
    assert len(ev["candidates"]) == 50
    assert ev["capped"] == 10
    assert ev["total_candidates"] == 60


def test_inject_finalize_writes_audit(core, scope):
    """注入路径旁路写 audit.jsonl：候选带 已注入/被剔 标注。"""
    core = core
    # 灌一条记忆；query 用记忆原文（BM25 必命中，保证正常链有注入）
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    inj = core.inject_finalize(scope, "我只看允许远程的岗位")
    assert inj.items  # 有注入

    events = audit_mod.load_events(None, account_id="test")
    assert events
    ev = events[-1]
    assert ev["event"] == "audit.retrieval"
    assert ev["query"]
    assert ev["injected_ids"]
    # 候选全集里：至少有一条注入、其余要么被剔要么未注入
    assert any(c["injected"] for c in ev["candidates"])


def test_explain_answers_why_not_used(core, scope):
    """explain 能答"那条为什么没进"（含超出注入条数的候选）。"""
    core = core
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    core.write(scope, "我不投需要长期出差的岗位", kind="preference")
    inj = core.inject_finalize(scope, "我只看允许远程的岗位")
    assert inj.items

    events = audit_mod.load_events(None, account_id="test")
    assert events

    # 构造一个最小轨迹（think 步用注入结果）
    trace = {
        "task": "我找岗位时有哪些硬性限制？",
        "exit": "completed",
        "home": str(core.home),
        "scope": {"account": "test", "session": "s1"},
        "steps": [
            {
                "node": "think",
                "step": 1,
                "action": "answer",
                "policy": "rule",
                "injected_ids": inj.injected_ids,
                "memory_lines": [f"[{i.kind}] {i.content}" for i in inj.items],
                "dropped": [{"id": "x", "reason": "已被更新的记忆取代", "score": 0.0}],
            }
        ],
    }
    from hippocampus.explain import explain_step

    text = explain_step(trace, 1, events)
    assert "候选全集（top-50 审计）" in text
    assert "已注入" in text
    # 能答"为什么没进"：被剔或未注入的候选都有标注
    assert ("被剔除" in text) or ("未注入" in text)


def test_explain_merges_observe_view(core, scope):
    """explain --run（无 --step）把 observe 与轨迹合并成一份 run 视图。"""
    from hippocampus.explain import explain_run
    from hippocampus.memory import observe_log

    core = core
    core.write(scope, "我每周三晚上固定留给项目开发", kind="preference")
    observe_log.log_injection("test", "周三做什么", ["m1"])
    observe_log.log_confirmation("test", ("confirm", "m2"), winner_id="m2", loser_ids=["m1"])

    trace = {
        "task": "我每周三晚上固定留给项目开发",
        "exit": "completed",
        "home": str(core.home),
        "scope": {"account": "test", "session": "s1"},
        "steps": [{"node": "think", "step": 1, "action": "answer", "policy": "rule",
                   "injected_ids": [], "memory_lines": [], "dropped": []}],
    }
    observe_events = observe_log_events(core.home, "test")
    out = explain_run(trace, observe_events=observe_events)
    assert "记忆层观察（observe.jsonl）合并视图" in out
    assert "[观察·注入]" in out


def observe_log_events(home, account):
    path = home / "accounts" / account / "observe.jsonl"
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_audit_timing_baseline(core, scope):
    """开关前后耗时基准：审计开启的开销有界（宽松上限，CI 安全）。"""
    core = core
    core.write(scope, "我只看允许远程的岗位", kind="preference")

    _toggle_audit(core, scope, False)
    t0 = time.perf_counter()
    for _ in range(10):
        core.inject_finalize(scope, "我投简历有什么硬性限制？")
    off_time = time.perf_counter() - t0

    _toggle_audit(core, scope, True)
    t0 = time.perf_counter()
    for _ in range(10):
        core.inject_finalize(scope, "我投简历有什么硬性限制？")
    on_time = time.perf_counter() - t0

    # 词法档下审计是一次同量级检索：开销应明显小于关闭时的耗时本身
    assert on_time < off_time * 3 + 0.5, f"审计开销过大: off={off_time:.3f}s on={on_time:.3f}s"
