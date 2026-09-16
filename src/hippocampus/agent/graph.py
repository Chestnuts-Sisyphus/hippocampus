"""Agent 形态：LangGraph 三节点编排（think → act → answer）。

编排层只写"记忆接线"与收口纪律，图的执行、状态、检查点、人在环中断由 LangGraph 提供
（方案 §十七-2：不重复造框架已有的东西）。

三出口（验收 A2）：
- `completed`        任务完成（有依据地答完／动作做完）
- `cannot_complete`  做不了（无依据、工具被拒、依赖缺失）——**明说做不了，不编**
- `needs_human`      需要人升级处理（危险动作未获确认、需登录/权限等）

记忆驱动四可观察点（验收 A30）落在本图的接线上：
1. `think` 前检索并注入 → 注入内容进入决策与作答（可观察：记忆关掉后行为变化）
2. `answer` 后固化 → 本轮确定的结论写回记忆（由 agent 产生，不是人工写入）
3. 冲突 → 挂起确认 → 确认后后续行为改变
4. 生命周期 → 降级/归档的记忆不进注入集合
"""

from __future__ import annotations

import re
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from hippocampus.agent.policy import Decision, ModelPolicy, RulePolicy
from hippocampus.agent.tools import ToolBox
from hippocampus.core import MemoryCore, Scope


class AgentState(TypedDict, total=False):
    task: str
    step: int
    max_steps: int
    decisions: Annotated[list[dict[str, Any]], lambda a, b: (a or []) + (b or [])]
    tool_calls: Annotated[list[dict[str, Any]], lambda a, b: (a or []) + (b or [])]
    last_tool: dict[str, Any]
    memory_lines: list[str]
    scored: list[tuple[float, str]]
    injected_ids: list[str]
    answer: str
    exit: str
    exit_reason: str
    pending: int


def _memory_lines(
    core: MemoryCore, scope: Scope, query: str
) -> tuple[list[str], list[str], list[dict[str, Any]], list[tuple[float, str]]]:
    """检索 + 装配注入；返回（可读行, 注入 id, 被剔候选, [(分数, 通道)…]）。

    分数与**通道**一起回传：策略层的证据闸要区分"命中通道"——图通道是"顺带想起
    同实体的东西"（分数离散 0.5/0.3/0.15，且会话实体兜底会带上本会话的实体），
    它**不构成"回答了这个问题"的证据**（见 RulePolicy.floor 的说明）。
    """
    injection = core.inject_finalize(scope, query)
    if not injection.enabled:
        return [], [], [], []
    lines = [f"[{item.kind}] {item.content}" for item in injection.items]
    dropped = [{"id": d.id, "reason": d.reason, "score": round(d.score, 4)} for d in injection.dropped]
    scored = [(float(item.score), str(item.channel or "")) for item in injection.items]
    return lines, list(injection.injected_ids), dropped, scored


_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\s，。；、）)】]+)|(?:/(?:home|Users)/[^\s，。；、）)】]+)")


def _sanitize(text: str) -> str:
    r"""固化前抹掉本机绝对路径。

    为什么：agent 的结论里常带"已写入 D:\AI\...\x.md"这类本机路径，把它写进记忆
    等于把个人机器的目录结构永久留在记忆里——既是隐私问题，也让记忆换机不可移植。
    记忆里留"已写入某文件"这个事实就够了。
    """
    return _ABS_PATH_RE.sub("（本地路径）", text or "")


def build_graph(core: MemoryCore, scope: Scope, *, tools: ToolBox, policy: Any, recorder: list[dict[str, Any]]):
    """构造三节点图。recorder 收集轨迹（可复演）。"""

    def think(state: AgentState) -> dict[str, Any]:
        step = int(state.get("step", 0)) + 1
        task = state.get("task", "")
        lines, injected, dropped, scored = _memory_lines(core, scope, task)
        decision: Decision = policy.decide(
            task,
            step=step,
            memory_lines=lines,
            scored=state.get("scored") or scored,
            last_tool=state.get("last_tool"),
            tools=tools.describe(),
        )
        recorder.append(
            {
                "node": "think",
                "step": step,
                "action": decision.action,
                "args": decision.args,
                "why": decision.why,
                "injected_ids": injected,
                "memory_lines": lines,
                "dropped": dropped,
                "policy": policy.name,
            }
        )
        return {
            "step": step,
            "decisions": [decision.to_dict()],
            "memory_lines": lines,
            "injected_ids": injected,
            "scored": state.get("scored") or scored,
        }

    def act(state: AgentState) -> dict[str, Any]:
        decision = (state.get("decisions") or [{}])[-1]
        action = decision.get("action", "")
        args = decision.get("args") or {}
        result = tools.call(action, args)
        recorder.append({"node": "act", "step": state.get("step", 0), "tool": action, "result": result})
        out: dict[str, Any] = {"last_tool": result, "tool_calls": [{"tool": action, "ok": result.get("ok", False)}]}
        if result.get("ok") and action == "search_memory":
            items = (result.get("result") or {}).get("items") or []
            if items:
                out["memory_lines"] = [f"[{i['kind']}] {i['content']}" for i in items]
                out["scored"] = [(float(i.get("score") or 0.0), str(i.get("channel") or "")) for i in items]
        if not result.get("ok") and result.get("category") == "被拒":
            out["exit"] = "needs_human"
            out["exit_reason"] = f"危险动作未获确认：{action}"
        return out

    def answer(state: AgentState) -> dict[str, Any]:
        decision = (state.get("decisions") or [{}])[-1]
        args = decision.get("args") or {}
        lines = state.get("memory_lines") or []
        if decision.get("action") == "answer":
            text = str(args.get("text") or "").strip()
            insufficient = bool(args.get("insufficient"))
        else:
            tool_result = state.get("last_tool") or {}
            if tool_result.get("ok"):
                text = f"已完成：{decision.get('action')}"
                if decision.get("action") == "list_memories":
                    items = (tool_result.get("result") or {}).get("items") or []
                    text = "当前生效的记忆：\n" + "\n".join(f"- [{i['kind']}] {i['content']}" for i in items)
                elif decision.get("action") == "write_file":
                    text = f"已写入 {tool_result.get('result', {}).get('path')}"
                elif decision.get("action") == "remember":
                    text = "已记下。"
                insufficient = False
            else:
                text = f"无法完成：{tool_result.get('error') or '工具执行失败'}"
                insufficient = True

        # 出口判定
        exit_kind = state.get("exit") or ""
        reason = state.get("exit_reason") or ""
        if not exit_kind:
            if insufficient:
                exit_kind = "cannot_complete"
                if decision.get("action") == "answer":
                    reason = "记忆中没有依据" if not lines else "检索命中不足以作为依据（低于证据线）"
                else:
                    reason = "工具未成功"
            else:
                exit_kind, reason = "completed", "有依据并给出结论"

        # 记忆固化（A30-②）：把本轮的结论写回；写不写由出口决定（做不了就不写）。
        # 来源轨纪律：agent 自己推导出的结论属**模型轨**（shadow=1，永不注入），
        # 只有用户原话／显式"记下"才进正式记忆（双轨隔离，验收 A8）。
        consolidated = 0
        if exit_kind == "completed" and lines and state.get("task"):
            outcome = core.write(
                scope,
                f"本轮任务结论：{_sanitize(text)[:200]}",
                kind="status",
                source_quote=f"任务：{state.get('task', '')[:120]}",
                source="model",
            )
            consolidated = len(outcome.ids)
            recorder.append(
                {
                    "node": "answer",
                    "step": state.get("step", 0),
                    "consolidated_ids": outcome.ids,
                    "exit": exit_kind,
                }
            )
        recorder.append(
            {
                "node": "answer",
                "step": state.get("step", 0),
                "text": text,
                "exit": exit_kind,
                "reason": reason,
                "consolidated": consolidated,
            }
        )
        return {"answer": text, "exit": exit_kind, "exit_reason": reason, "pending": len(core.pending(scope))}

    def route_after_think(state: AgentState) -> str:
        decision = (state.get("decisions") or [{}])[-1]
        if decision.get("action") == "answer":
            return "answer"
        return "act"

    def route_after_act(state: AgentState) -> str:
        if state.get("exit"):
            return "answer"  # 需人升级：直接收口，报明原因
        if int(state.get("step", 0)) >= int(state.get("max_steps", 8)):
            return "answer"
        decision = (state.get("decisions") or [{}])[-1]
        if decision.get("action") == "search_memory" and (state.get("memory_lines") or []):
            return "think"  # 拿到记忆后重新决策（用记忆里的依据作答）
        return "answer"

    graph = StateGraph(AgentState)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_node("answer", answer)
    graph.set_entry_point("think")
    graph.add_conditional_edges("think", route_after_think, {"act": "act", "answer": "answer"})
    graph.add_conditional_edges("act", route_after_act, {"think": "think", "answer": "answer"})
    graph.add_edge("answer", END)
    return graph


__all__ = ["AgentState", "ModelPolicy", "RulePolicy", "build_graph"]
