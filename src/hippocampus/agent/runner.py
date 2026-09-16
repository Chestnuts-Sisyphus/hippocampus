"""Agent 形态的入口：跑一个任务、出轨迹、可复演。

先冻结的一条纪律：**轨迹要能重跑，不是录像**。`replay()` 在离线档用同一张图、同一策略
重跑同一任务，两次结果必须逐字节可比（时间戳字段除外，A22）。

确定性来源：① 离线档策略是规则策略（无随机）；② 检索排序是确定性的（分数 + 新鲜度 + id）；
③ 轨迹里只有工具结果与决策，没有模型采样。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippocampus.agent.graph import RulePolicy, build_graph
from hippocampus.agent.tools import ToolBox
from hippocampus.core import MemoryCore, Scope

EXITS = ("completed", "cannot_complete", "needs_human")
# 指纹比较时排除的字段（两类）：
#   ① 时间戳（本来就不影响"同输入同轨迹"的判断）；
#   ② **本轮新生成的标识符**——新写入记忆的 id 是 uuid，两轮必然不同，但它不代表
#      行为差异（"是否发生了固化"由 consolidated 计数体现，已有）。
_TS_KEYS = ("ts", "elapsed_ms", "started_at", "finished_at", "consolidated_ids", "episode_id", "home")


@dataclass
class RunResult:
    task: str
    exit: str
    answer: str
    steps: int
    trace: list[dict[str, Any]] = field(default_factory=list)
    injected_ids: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending: int = 0
    exit_reason: str = ""

    def render(self) -> str:
        lines = [
            f"任务：{self.task}",
            f"出口：{self.exit}（{self.exit_reason}）  步数：{self.steps}",
            f"注入记忆：{len(self.injected_ids)} 条  工具调用：{len(self.tool_calls)} 次  待确认：{self.pending}",
            "",
            self.answer,
        ]
        return "\n".join(lines)


@dataclass
class ReplayResult:
    times: int
    consistent: bool
    digests: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"确定性复演：{'两次运行轨迹一致 ✓' if self.consistent else '两次运行轨迹不一致 ✗'}"
        return "\n".join([head, f"  复演次数：{self.times}", f"  指纹：{', '.join(self.digests)}", f"  出口：{', '.join(self.exits)}"])


def run_task(
    core: MemoryCore,
    scope: Scope,
    task: str,
    *,
    offline: bool = False,
    max_steps: int = 8,
    workdir: Path | None = None,
    allow_write: bool = True,
    confirm: Any = None,
) -> RunResult:
    """跑一个任务。offline=True 时用规则策略（无模型、确定性）。"""
    if not task:
        task = "（空任务）"
    recorder: list[dict[str, Any]] = []
    toolbox = ToolBox(core, scope, workdir=workdir, allow_write=allow_write, confirm=confirm)
    if offline:
        policy: Any = RulePolicy()
    else:
        from hippocampus.agent.policy import ModelPolicy
        from hippocampus.settings import llm_available

        policy = RulePolicy() if not llm_available() else ModelPolicy()

    graph = build_graph(core, scope, tools=toolbox, policy=policy, recorder=recorder).compile()
    final = graph.invoke({"task": task, "step": 0, "max_steps": max_steps, "decisions": [], "tool_calls": []})
    trace = {
        "task": task,
        "policy": policy.name,
        "offline": bool(offline),
        "steps": recorder,
        "exit": final.get("exit", ""),
        "answer": final.get("answer", ""),
        # 复演所需上下文（replay 用同一 scope 重跑同一任务）
        "scope": {"account": scope.account, "session": scope.session},
        "home": str(core.home),
        "max_steps": max_steps,
    }
    injected: list[str] = []
    for row in recorder:
        injected.extend(row.get("injected_ids") or [])
    return RunResult(
        task=task,
        exit=str(final.get("exit") or "cannot_complete"),
        answer=str(final.get("answer") or ""),
        steps=int(final.get("step") or 0),
        trace=trace,
        injected_ids=sorted(set(injected)),
        tool_calls=[c for c in (final.get("tool_calls") or [])],
        pending=int(final.get("pending") or 0),
        exit_reason=str(final.get("exit_reason") or ""),
    )


def _digest(trace: dict[str, Any]) -> str:
    import hashlib
    import json

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in sorted(obj.items()) if k not in _TS_KEYS}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    payload = json.dumps(scrub(trace), ensure_ascii=False, sort_keys=True)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def replay(data: dict[str, Any], *, times: int = 2, home: str | None = None) -> ReplayResult:
    """用记录重跑：结果指纹逐次比较（不是播放录像，是真正重跑同一图）。

    确定性要求"同一初始状态 + 同一输入 → 同一轨迹"：所以**每轮都从同一份库副本出发**
    （上一轮跑完会写库——固化本身就改状态），否则第二次跑的就是被第一次改过的世界，
    比较没有意义。
    """
    import shutil
    import tempfile

    task = str(data.get("task") or "")
    scope_data = data.get("scope") or {}
    source_home = Path(home or data.get("home") or "")
    scope = Scope(
        account=str(scope_data.get("account") or "replay"),
        session=str(scope_data.get("session") or "replay"),
    )
    workdir = Path(tempfile.mkdtemp(prefix="hippo_replay_"))
    digests: list[str] = []
    exits: list[str] = []
    for _ in range(max(times, 2)):
        replay_home = workdir / "home"
        if replay_home.exists():
            shutil.rmtree(replay_home, ignore_errors=True)
        replay_home.mkdir(parents=True, exist_ok=True)
        if source_home and source_home.exists():
            shutil.copytree(source_home, replay_home, dirs_exist_ok=True)
        core = MemoryCore(home=replay_home)
        run = run_task(core, scope, task, offline=True, max_steps=int(data.get("max_steps") or 8))
        # 指纹里的 home 是临时路径，逐轮不同但**语义相同**，统一归一后再比
        trace = dict(run.trace)
        trace["home"] = "<replay-home>"
        digests.append(_digest(trace))
        exits.append(run.exit)
        core.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return ReplayResult(times=times, consistent=len(set(digests)) == 1, digests=digests, exits=exits)


__all__ = ["EXITS", "ReplayResult", "RunResult", "replay", "run_task"]
