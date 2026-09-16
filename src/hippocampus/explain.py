"""`explain`：回答"这一步为什么注入这几条、那条为什么没进"（验收 A18）。

**不靠模型编**：解释文本由**轨迹里记录的事实**重建——每一步都记了
`injected_ids`、`memory_lines`、`dropped`（含每条被剔候选的理由与分数）。
这个能力补的正是前身的观测缺口（前身 `observe.jsonl` 只记 `injected_ids`，
事后无法回答"某条为什么没进来"）。

v1 形态：读轨迹 JSON（`hippocampus chat --trace out.json` 产出的），逐条列证据。
P2 会把它升级为常驻 AuditSink（top-N=50 的候选全集），口径不变。
"""

from __future__ import annotations

from typing import Any

TEMPLATE = """第 {step} 步（节点 {node}，策略 {policy}）
  动作：{action}
  注入 {injected} 条：
{injected_lines}
  候选被剔除 {dropped} 条：
{dropped_lines}
"""


def _fmt_injected(lines: list[str]) -> str:
    if not lines:
        return "    （无）"
    return "\n".join(f"    - {line}" for line in lines)


def _fmt_dropped(dropped: list[dict[str, Any]]) -> str:
    if not dropped:
        return "    （无被剔候选）"
    return "\n".join(f"    - {d.get('id', '?')}：{d.get('reason', '（未记录理由）')}" for d in dropped)


def explain_step(trace: dict[str, Any], step: int) -> str:
    """解释轨迹里某一步（按 think 节点的序号）。"""
    steps = [s for s in (trace.get("steps") or []) if s.get("node") == "think"]
    if not steps:
        return "该轨迹没有可解释的步骤（think 节点为空）。"
    picked = next((s for s in steps if int(s.get("step") or 0) == step), None)
    if picked is None:
        available = ", ".join(str(s.get("step")) for s in steps)
        return f"没有第 {step} 步。可解释的步骤：{available}"

    injected_lines = list(picked.get("memory_lines") or [])
    dropped = list(picked.get("dropped") or [])
    body = TEMPLATE.format(
        step=picked.get("step"),
        node="think",
        policy=picked.get("policy", "?"),
        action=picked.get("action", "?"),
        injected=len(injected_lines),
        injected_lines=_fmt_injected(injected_lines),
        dropped=len(dropped),
        dropped_lines=_fmt_dropped(dropped),
    )
    head = f"任务：{trace.get('task', '（未记录）')}\n出口：{trace.get('exit', '?')}\n\n"
    tail = "\n（本解释由轨迹记录重建，不由模型生成；被剔理由来自检索过滤链的判定结果。）"
    return head + body + tail


def explain_run(trace: dict[str, Any]) -> str:
    """整条轨迹逐步解释。"""
    steps = [s for s in (trace.get("steps") or []) if s.get("node") == "think"]
    if not steps:
        return "该轨迹没有可解释的步骤。"
    return "\n".join(explain_step(trace, int(s.get("step") or 0)) for s in steps)


__all__ = ["explain_run", "explain_step"]
