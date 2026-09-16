"""验收 A22（复演一致）／A1（真实任务跑通并出轨迹）／A2（三出口可演示）／A36（离线档）。

这些用例都跑在**无凭据 + 离线**下（conftest 默认如此），所以它们同时证明了
"没有模型也能跑记忆纪律"。
"""

from __future__ import annotations

import json
from pathlib import Path

from hippocampus.agent.runner import run_task
from hippocampus.explain import explain_step
from hippocampus.seed import seed


def test_a1_real_task_produces_trace(core, scope, workdir):
    """① 真实任务跑通并出轨迹（轨迹里每一步都能复盘）。"""
    seed(core, scope)
    run = run_task(core, scope, "我找岗位时有哪些硬性限制？", offline=True, workdir=workdir)

    assert run.exit == "completed"
    assert run.steps >= 2
    nodes = [s.get("node") for s in run.trace["steps"]]
    assert "think" in nodes and "act" in nodes and "answer" in nodes, "三节点都应留下痕迹"
    assert run.trace["steps"][0]["action"] == "search_memory"
    # 轨迹可落盘、可再读（CLI --trace 的路径）
    path = Path(workdir) / "trace.json"
    path.write_text(json.dumps(run.trace, ensure_ascii=False), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["task"] == run.task


def test_a2_three_exits(core, scope, workdir):
    """② 三出口：完成／无法完成／需人工升级。"""
    seed(core, scope)

    completed = run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)
    assert completed.exit == "completed"

    cannot = run_task(core, scope, "帮我写一首关于秋天的诗", offline=True, workdir=workdir)
    assert cannot.exit == "cannot_complete"
    assert "没有相关" in cannot.answer or "无法确认" in cannot.answer, "做不了要明说，不许编"

    # 危险动作未获确认 → 需人工升级
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=False)
    denied = box.call("write_file", {"path": "x.md", "content": "hi"})
    assert denied["category"] == "被拒"  # 对应 needs_human 的判定输入

    needs_human = run_task(
        core, scope, "把我的岗位偏好清单写成 markdown 文件", offline=True, workdir=workdir, allow_write=False
    )
    assert needs_human.exit == "needs_human"


def test_a22_replay_is_deterministic(core, scope, home, workdir):
    """③ 复演一致：两次重跑轨迹指纹相同（不是播放录像，是真重跑）。"""
    from hippocampus.agent.runner import replay

    seed(core, scope)
    run = run_task(core, scope, "我找岗位时有哪些硬性限制？", offline=True, workdir=workdir)
    result = replay(run.trace, times=2, home=str(home))
    assert result.consistent, f"复演不一致：{result.digests}"
    assert len(set(result.exits)) == 1


def test_a36_offline_memory_discipline(core, scope, workdir):
    """④ 离线档记忆纪律可用：无凭据、无网络，写入/检索/注入/固化全通。"""
    from hippocampus.memory import llm

    assert llm.available() is False, "测试环境必须无凭据（conftest 保证）"

    # 句式规则抽取：不需要模型
    turn = core.consolidate(scope, user_text="我只投含 MCP 的岗位。我不看外包。")
    assert turn.write.ids, "离线档也应能从显式句式里抽取并入库"
    contents = [m.content for m in core.list_memories(scope, limit=10)]
    assert "我不看外包" in contents

    # 检索与注入照常
    injection = core.inject_finalize(scope, "我的岗位筛选条件是什么")
    assert injection.items

    # 冲突挂起与确认照常
    core.write(scope, "我的期望城市是北京", kind="preference")
    core.write(scope, "我的期望城市是杭州", kind="preference")
    assert core.pending(scope), "离线档也要能挂起冲突"

    # 开关照常
    assert "已关闭" in (core.set_switch(scope, "关闭记忆") or "")
    assert core.inject_finalize(scope, "任意问题").enabled is False
    core.set_switch(scope, "打开记忆")


def test_explain_answers_why(core, scope, workdir):
    """⑤ 可追责：能回答"注入了什么、为什么那条没进"。"""
    seed(core, scope)
    run = run_task(core, scope, "我投简历有什么要求？", offline=True, workdir=workdir)
    text = explain_step(run.trace, 1)
    assert "注入" in text
    assert "被剔" in text
    assert "不由模型生成" in text


def test_agent_does_not_touch_memory_internals():
    """形态层只经 MemoryCore 访问记忆（架构约束的可执行版本）。"""
    import hippocampus.agent.graph as graph_mod
    import hippocampus.agent.runner as runner_mod
    import hippocampus.agent.tools as tools_mod
    import hippocampus.proxy.app as proxy_mod

    for module in (graph_mod, runner_mod, tools_mod, proxy_mod):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "hippocampus.memory" not in source.replace("hippocampus.memory.llm", ""), (
            f"{module.__name__} 直接 import 了记忆层内部模块：形态层必须只经 hippocampus.core"
        )


def test_cli_demo_runs_offline(core, scope, workdir, monkeypatch):
    """`hippocampus demo` 在离线档可跑（A38 的最小版：无 key 无网出结果表）。"""
    from hippocampus.cli import main

    monkeypatch.setattr("sys.argv", ["hippocampus", "--home", str(core.home), "--account", scope.account, "demo"])
    exit_code = main(["--home", str(core.home), "--account", scope.account, "demo", "--questions", "5"])
    assert exit_code == 0
