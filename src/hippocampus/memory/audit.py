"""旁路审计通道 AuditSink（A18 完整形态，缺口清单 D1）。

- 位置：账户数据目录下 `audit.jsonl`（与 `observe.jsonl` 并列，独立旁路文件）。
- 内容：每次注入检索的**候选全集**（top-N=50 上限，超限只记计数）——每个候选带
  doc_id / kind / channel / score / 是否注入 / 被剔理由。
- 用途：`explain` 回答"那条为什么没进"（含**超出检索 top-k 的候选**，这是旧版
  `explain` 读轨迹 JSON 看不到的部分）；与 `observe.jsonl`（注入/确认事件）合并成
  一份 run 视图。
- 纪律：**旁路**——写失败只记 stderr，绝不影响注入主流程；开关见活跃快照的
  `audit_enabled`（默认开，`INJECTION_PARAM_DEFAULTS`）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hippocampus.memory import account

# 候选全集上限（A18 口径：top-N=50；超出的只记计数，不落盘）
TOP_N = 50


def audit_path(account_id: str) -> Path:
    """审计日志文件路径（账户数据目录下 audit.jsonl）。"""
    return account.get_account_data_dir(account_id) / "audit.jsonl"


def _append(account_id: str, event: dict[str, Any]) -> None:
    """追加一条审计事件（D1：按 `observability` 配置做**大小轮转**；软失败不中断主流程）。"""
    from hippocampus.memory import jsonl_log

    jsonl_log.append_jsonl(audit_path(account_id), event)


def record_retrieval(
    account_id: str,
    *,
    query: str,
    candidates: list[dict[str, Any]],
    injected_ids: list[str],
) -> None:
    """记录一次注入检索的候选全集。

    candidates 顺序即排名顺序；`injected` 标记本次实际注入；`dropped`＋`reason`
    给出被剔候选的理由。超过 TOP_N 的候选只计入 `capped` 计数，不落盘。
    """
    top = candidates[:TOP_N]
    _append(
        account_id,
        {
            "event": "audit.retrieval",
            "ts": int(time.time() * 1000),
            "query": (query or "")[:300],
            "candidates": top,
            "injected_ids": injected_ids[:50],
            "total_candidates": len(candidates),
            "capped": max(0, len(candidates) - TOP_N),
        },
    )


def load_events(
    path: str | Path | None, account_id: str | None = None, *, include_rotated: bool = False
) -> list[dict[str, Any]]:
    """读审计 JSONL（无文件 → 空列表；坏行跳过）。

    `include_rotated=True` 时连同滚动份（`audit.jsonl.1/.2…`）按"旧→新"一起读——
    复盘"上周那次注入为什么没进"时用得着（D1 轮转后历史不会凭空消失）。
    """
    from hippocampus.memory import jsonl_log

    if path is None:
        path = audit_path(account_id) if account_id else None
    if not path:
        return []
    return jsonl_log.load_events(path, include_rotated=include_rotated)


__all__ = ["TOP_N", "audit_path", "load_events", "record_retrieval"]
