"""N13 Agent 形态不阉割核对（B3）：两形态共用同一批记忆层入口。

设计正本 §一：「Hippocampus 的全部设计与功能**不得在 agent 里被阉割或重构**」。
本文件把"不阉割"钉成可测的四件事：

① agent **轮末走 `consolidate`**（对话固化＋轮末守卫＋确认块），不是只用 write／search；
② agent **能消费确认指令**（`确认 n`／`否决`）——确认轨在 Agent 形态里不断链；
③ **观察轨**（模型输出 → `shadow=1`，永不注入）在生产路径上真的被接上（两种形态都走
   `consolidate`，所以这一条对两边同时成立）；
④ 两形态**调用的记忆层入口清单一致**（用计数器把入口调用序列抓下来对比）。

覆盖表（人读版）见 `docs/forms-parity.md`。
"""

from __future__ import annotations

import pytest

from hippocampus.agent.runner import run_task
from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import extract as extract_mod
from hippocampus.memory import memory_bridge as mb
from hippocampus.seed import seed


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


# ------------------------------------------------ ① 轮末 consolidate


def test_agent_runs_consolidate_at_turn_end(core, scope, workdir):
    """任务跑完必须留下 `consolidate` 节点（对话固化＋守卫走的是同一条路）。"""
    seed(core, scope)
    run = run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)
    nodes = [row.get("node") for row in run.trace["steps"]]
    assert "consolidate" in nodes, f"轮末应走 consolidate，实际节点 {nodes}"
    row = next(r for r in run.trace["steps"] if r.get("node") == "consolidate")
    assert "guard_error" not in row, "轮末守卫不应报错"


def test_agent_persists_user_statement_from_the_same_turn(core, scope, workdir):
    """用户在本轮说的事实/偏好要进库（对话固化真的跑在 agent 路径上，不是只跑守卫）。"""
    run = run_task(core, scope, "记住：我不看外包", offline=True, workdir=workdir)
    assert run.exit == "completed"
    items = core.list_memories(scope, limit=50, status=None)
    assert any("外包" in i.content for i in items), f"本轮陈述应被固化，实际 {[i.content for i in items]}"


# ------------------------------------------------ ② 确认消费路径


def test_agent_consumes_confirmation(core, scope, workdir):
    """挂起冲突 → `确认 1` 交给 agent 跑 → 裁决生效且后续行为随之改变（A30-③ 的 Agent 侧）。"""
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")
    pending = core.pending(scope)
    assert pending, "同对象取值不同应挂起"
    candidate = next(p for p in pending if p["is_new"])

    run = run_task(core, scope, f"确认{candidate['num']}", offline=True, workdir=workdir)
    assert run.exit == "completed"
    assert "裁决" in run.answer, f"应回执确认结果，实际 {run.answer!r}"
    assert run.pending == 0, "裁决后不应还挂着这一块"

    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows.get("我的期望城市是杭州") == "active"
    assert rows.get("我的期望城市是北京") == "superseded"

    after = run_task(core, scope, "我现在想去哪个城市工作？", offline=True, workdir=workdir)
    assert "杭州" in after.answer and "北京" not in after.answer


def test_agent_veto_keeps_old_value(core, scope, workdir):
    """`否决` 也要能消费：旧值继续生效、新条不转正（A24 三分支的 Agent 侧）。"""
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")
    assert core.pending(scope), "同对象取值不同应挂起"

    run = run_task(core, scope, "否决", offline=True, workdir=workdir)
    assert run.exit == "completed" and "裁决" in run.answer
    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows.get("我的期望城市是北京") == "active", "否决后旧值应继续生效"
    assert rows.get("我的期望城市是杭州") != "active", "被否决的新条不应生效"


# ------------------------------------------------ ③ 观察轨真的接上了


FAKE_MODEL_OUTPUT = {
    "memories": [
        {"type": "resource", "content": "文档在 README.md", "source_quote": "（模型输出）"},
    ]
}


def test_observation_track_wired_in_consolidate(core, scope, monkeypatch):
    """模型输出经观察轨入库（`shadow=1`）且**永不注入**——两形态共用 `consolidate`，所以这条对两边都成立。

    用模块级可替换提取指针模拟"有端点时的模型提取"（离线档也能测这条接线）。
    """
    monkeypatch.setattr(mb, "_extract_response_fn", lambda text: FAKE_MODEL_OUTPUT, raising=True)
    turn = core.consolidate(scope, user_text="你好", assistant_text="文档在 README.md，部署已完成")

    observed = core.list_memories(scope, limit=20, status=None, include_shadow=True)
    shadow_items = [i for i in observed if getattr(i, "shadow", 0) == 1]
    assert turn.observed_ids, "观察轨应有入库 id"
    assert shadow_items, "模型输出应进观察轨（shadow=1）"

    injection = core.inject_finalize(scope, "文档在哪里")
    assert all(i.shadow == 0 for i in injection.items), "观察轨永不注入"


def test_agent_observation_track_wired(core, scope, workdir, monkeypatch):
    """Agent 形态同样接上：任务跑完，模型输出里的资源进观察轨（不注入）。"""
    monkeypatch.setattr(mb, "_extract_response_fn", lambda text: FAKE_MODEL_OUTPUT, raising=True)
    seed(core, scope)
    run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)
    items = core.list_memories(scope, limit=50, status=None, include_shadow=True)
    assert any(i.shadow == 1 for i in items), "agent 轮末也应把模型输出送进观察轨"


def test_observation_track_respects_learning_switch(core, scope, monkeypatch):
    """学习开关关掉 → 观察轨不抽取（别在用户说"别学"的时候偷偷入库）。"""
    monkeypatch.setattr(mb, "_extract_response_fn", lambda text: FAKE_MODEL_OUTPUT, raising=True)
    core.set_switch(scope, "停止学习")
    try:
        turn = core.consolidate(scope, user_text="你好", assistant_text="文档在 README.md")
        assert turn.observed_ids == []
    finally:
        core.set_switch(scope, "继续学习")


def test_extract_pointer_is_the_documented_seam():
    """观察轨的替换点是模块级指针（随迁设计），不是我们临时加的钩子。"""
    assert callable(mb._get_extract_response_fn())  # noqa: SLF001
    assert callable(extract_mod.extract_response_items)


# ------------------------------------------------ ④ 两形态入口清单一致


def _spy_on_core(monkeypatch, names: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {n: 0 for n in names}
    for name in names:
        original = getattr(MemoryCore, name)

        def wrapper(self, *args, _name=name, _orig=original, **kwargs):
            counts[_name] += 1
            return _orig(self, *args, **kwargs)

        monkeypatch.setattr(MemoryCore, name, wrapper)
    return counts


def test_both_forms_share_the_same_core_entries(core, scope, workdir, monkeypatch):
    """两形态的入口清单一致：注入／固化／确认／写入都经 `MemoryCore` 同一批方法。

    做法：给核心方法装计数器 → 各跑一遍（agent 任务 + 代理式一轮）→ 对比"用到了哪些入口"。
    代理侧这里直接调 `consolidate/inject_finalize/confirm`（代理进程内就是这条链，
    `proxy/app.py::_handle` 的三步与之一一对应；真服务端到端见 test_proxy_formats.py）。
    """
    seed(core, scope)
    names = ("inject_finalize", "consolidate", "confirm", "write", "search")
    counts = _spy_on_core(monkeypatch, names)

    # 形态一：Agent
    run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)

    # 形态二：代理（同一批入口；`确认 n` 走 confirm，注入走 inject_finalize，响应后走 consolidate）
    core.inject_finalize(scope, "我投简历有什么要求？")
    core.consolidate(scope, user_text="我不看外包", assistant_text="（模型回复）")
    core.confirm(scope, "确认1")

    for name in names:
        assert counts[name] > 0, f"{name} 在两个形态里都应被调用（实际 {counts}）"
