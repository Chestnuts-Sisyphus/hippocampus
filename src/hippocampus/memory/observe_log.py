# 由前身 hippocampus_prototype/observe_log.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus A7 —— 观察落点（observe_log.py，HC-0815-02 节点6 新增）

隔离观察记录：账户目录下 observe.jsonl（追加写，与 memory.db 同目录但独立文件）。
记录两类事件供数据质量与洞察线衔接（本模块只产出文件指针，不写任何线手册）：

- injection：每次 prepare_injection 的 query / 实际注入记忆 id 列表 / 时间
- confirmation：确认块消费的 decision（confirm 的 winner / veto 的 loser 列表）/ 时间

约束：
- 禁写洞察线手册、禁写真实库（本模块只写账户目录 observe.jsonl，调用方保证
  传隔离账户）
- 软失败：记录异常记 stderr，绝不影响注入/确认主流程
"""

import json
import sys
from pathlib import Path
from typing import Any

from hippocampus.memory import account


def observe_path(account_id: str) -> Path:
    """观察日志文件路径（账户数据目录下 observe.jsonl）。"""
    return account.get_account_data_dir(account_id) / "observe.jsonl"


def _append(account_id: str, event: dict[str, Any]) -> None:
    try:
        path = observe_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write(f"[observe] 观察记录失败（不中断）: {e}\n")


def log_injection(account_id: str, query: str, injected_ids: list[str]) -> None:
    """注入观察：query + 实际注入的记忆/经历 id 列表。"""
    import time

    _append(
        account_id,
        {
            "event": "injection",
            "ts": int(time.time() * 1000),
            "query": (query or "")[:200],
            "injected_ids": injected_ids[:20],
        },
    )


def log_confirmation(
    account_id: str, decision: tuple, winner_id: str | None = None, loser_ids: list[str] | None = None
) -> None:
    """确认块消费观察：decision（('confirm', id) / ('veto', None)）+ 涉及记忆。"""
    import time

    _append(
        account_id,
        {
            "event": "confirmation",
            "ts": int(time.time() * 1000),
            "decision": decision[0],
            "winner_id": winner_id,
            "loser_ids": loser_ids or [],
        },
    )
