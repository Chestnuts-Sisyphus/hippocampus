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

import json
import sys
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
    try:
        path = audit_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"[audit] 审计记录失败（不中断）: {e}\n")


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


def load_events(path: str | Path | None, account_id: str | None = None) -> list[dict[str, Any]]:
    """读审计 JSONL（无文件 → 空列表；坏行跳过）。"""
    if path is None:
        path = audit_path(account_id) if account_id else None
    if not path or not Path(path).exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return []
    return out


__all__ = ["TOP_N", "audit_path", "load_events", "record_retrieval"]
