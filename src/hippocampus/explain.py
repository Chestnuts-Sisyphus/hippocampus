"""`explain`：回答"这一步为什么注入这几条、那条为什么没进"（验收 A18）。

**不靠模型编**：解释文本由**轨迹里记录的事实**重建——每一步都记了
`injected_ids`、`memory_lines`、`dropped`（含每条被剔候选的理由与分数）。

v2 形态（A18 完整版，缺口清单 D1/D2）：
1. **AuditSink 旁路**：`hippocampus memory` 的注入链会把每次检索的候选全集
   （top-N=50，超限只记计数）写进账户目录 `audit.jsonl`。`explain` 读它，就能
   回答"第 3 步为什么没用 X"——包括**超出检索 top-k 的候选**（旧版看不到的部分）。
2. **观测合并视图**：把 `observe.jsonl`（注入/确认事件）与 agent 轨迹合并成
   一份 run 视图——"记忆层视角"与"编排视角"同一份输出。

审计文件默认路径：`<轨迹里的 home>/accounts/<account>/audit.jsonl`；
可用 `--audit <path>` 显式指定。没有审计文件时退化为旧版轨迹解释（不报错）。
"""

from __future__ import annotations

from pathlib import Path
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


def _match_audit_events(step: dict[str, Any], audit_events: list[dict[str, Any]], task: str) -> list[dict[str, Any]]:
    """找出与某一步相关的审计事件：注入 id 有交集，或 query 与任务文本一致。

    一个 run 的多步共享同一任务 query，所以同一批事件可能匹配多步——
    这没问题：审计记录的是"这一次检索的候选全集"，同一查询的答案一致。
    """
    if not audit_events:
        return []
    step_ids = set(step.get("injected_ids") or [])
    out: list[dict[str, Any]] = []
    for ev in audit_events:
        if ev.get("event") != "audit.retrieval":
            continue
        ids = set(ev.get("injected_ids") or [])
        if step_ids and (ids & step_ids):
            out.append(ev)
        elif ev.get("query") and task and ev["query"] == task:
            out.append(ev)
    return out


def _fmt_audit(events: list[dict[str, Any]]) -> str:
    """渲染审计候选全集（top-N=50）：每个候选标注 已注入／被剔理由／未注入。"""
    if not events:
        return ""
    ev = events[-1]  # 同一查询取最近一次审计事件
    cands = ev.get("candidates") or []
    if not cands:
        return "    （审计无候选）"
    lines = []
    for c in cands:
        doc_id = c.get("doc_id", "?")
        kind = c.get("kind", "")
        channel = c.get("channel", "")
        score = c.get("score", 0.0)
        if c.get("injected"):
            mark = "已注入"
        elif c.get("dropped"):
            mark = f"被剔除：{c.get('reason') or '（未记录理由）'}"
        else:
            mark = "未注入（排名超出本轮注入上限或未被选中）"
        lines.append(f"    - {doc_id} [{kind}/{channel}] score={score}  {mark}")
    head = f"  候选全集（top-50 审计）{len(cands)} 个"
    capped = int(ev.get("capped") or 0)
    if capped:
        head += f"，另有 {capped} 个超出 top-50 未记录"
    head += "："
    return "\n".join([head, *lines])


def explain_step(
    trace: dict[str, Any],
    step: int,
    audit_events: list[dict[str, Any]] | None = None,
) -> str:
    """解释轨迹里某一步（按 think 节点的序号）。

    audit_events：该账户 `audit.jsonl` 的全部事件（由调用方读取，可缺省）。
    """
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
    audit_section = _fmt_audit(_match_audit_events(picked, audit_events or [], trace.get("task", "")))
    if audit_section:
        body += "\n" + audit_section
    head = f"任务：{trace.get('task', '（未记录）')}\n出口：{trace.get('exit', '?')}\n\n"
    tail = "\n（本解释由轨迹与审计记录重建，不由模型生成；被剔理由来自检索过滤链的判定结果。）"
    return head + body + tail


def _fmt_observe(events: list[dict[str, Any]]) -> str:
    """合并视图：observe.jsonl 的注入/确认事件（记忆层视角）。"""
    lines = []
    for ev in events:
        kind = ev.get("event")
        if kind == "injection":
            lines.append(f"    [观察·注入] 查询「{ev.get('query', '')}」→ 注入 {len(ev.get('injected_ids') or [])} 条")
        elif kind == "confirmation":
            dec = ev.get("decision")
            winner = ev.get("winner_id") or "（否决）"
            lines.append(f"    [观察·确认] {dec} → {winner}（涉及 {len(ev.get('loser_ids') or [])} 条败选）")
    return "\n".join(lines) if lines else "    （无观察事件）"


def explain_run(
    trace: dict[str, Any],
    audit_events: list[dict[str, Any]] | None = None,
    observe_events: list[dict[str, Any]] | None = None,
) -> str:
    """整条轨迹逐步解释；有审计/观察事件时合并成一份 run 视图。"""
    steps = [s for s in (trace.get("steps") or []) if s.get("node") == "think"]
    if not steps:
        return "该轨迹没有可解释的步骤。"
    parts = [explain_step(trace, int(s.get("step") or 0), audit_events) for s in steps]
    if observe_events:
        parts.append("记忆层观察（observe.jsonl）合并视图：\n" + _fmt_observe(observe_events))
    return "\n".join(parts)


def default_audit_path(trace: dict[str, Any]) -> Path:
    """审计文件默认位置：`<轨迹里的 home>/accounts/<account>/audit.jsonl`。"""
    home = Path(trace.get("home") or ".")
    account = (trace.get("scope") or {}).get("account") or "default"
    return home / "accounts" / account / "audit.jsonl"


__all__ = ["default_audit_path", "explain_run", "explain_step"]
