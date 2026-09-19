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

from pathlib import Path
from typing import Any

from hippocampus.memory import account


def observe_path(account_id: str) -> Path:
    """观察日志文件路径（账户数据目录下 observe.jsonl）。"""
    return account.get_account_data_dir(account_id) / "observe.jsonl"


def _append(account_id: str, event: dict[str, Any]) -> None:
    """追加一条观察事件（D1：与审计同一套大小轮转；软失败不中断主流程）。"""
    from hippocampus.memory import jsonl_log

    jsonl_log.append_jsonl(observe_path(account_id), event)


def log_injection(account_id: str, query: str, injected_ids: list[str], run_id: str = "") -> None:
    """注入观察：query + 实际注入的记忆/经历 id 列表 / run_id（九轮 X2）。"""
    import time

    _append(
        account_id,
        {
            "event": "injection",
            "ts": int(time.time() * 1000),
            "run_id": run_id,
            "query": (query or "")[:200],
            "injected_ids": injected_ids[:20],
        },
    )


def log_confirmation(
    account_id: str, decision: tuple, winner_id: str | None = None, loser_ids: list[str] | None = None, run_id: str = ""
) -> None:
    """确认块消费观察：decision（('confirm', id) / ('veto', None)）+ 涉及记忆 / run_id（九轮 X2）。"""
    import time

    _append(
        account_id,
        {
            "event": "confirmation",
            "ts": int(time.time() * 1000),
            "run_id": run_id,
            "decision": decision[0],
            "winner_id": winner_id,
            "loser_ids": loser_ids or [],
        },
    )


def log_verification(
    account_id: str, content: str, status: str, method: str = "", evidence: str = "", dropped: bool = False
) -> None:
    """求证事件（七轮 T2）：一次判定的状态／判据／证据摘要，以及是否因此**不进正式记忆**。

    A42 的核对点就在这里：模型幻觉出的资源被丢弃时，正式库里没有，但观察日志必须有痕。
    与其余事件同一纪律——只记摘要（内容截断 200 字），不落全文。"""
    import time

    _append(
        account_id,
        {
            "event": "verification",
            "ts": int(time.time() * 1000),
            "status": status,
            "method": method,
            "evidence": (evidence or "")[:200],
            "content_head": (content or "")[:200],
            "dropped": bool(dropped),
        },
    )
