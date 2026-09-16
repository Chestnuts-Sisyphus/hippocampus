# 由前身 hippocampus_prototype/observe.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 反馈环 N7 -- 观察期报告

三信号观察期报告（改前 vs 改后）：
  1. 重述率 = alarm 事件数 / first_mention 事件数
  2. 确认块响应率 = 用户确认/否决次数 / 确认块出现次数
  3. 检索零命中率 = retrieve 返回 0 条记忆的次数 / 总检索次数

回滚 = 标记 param_versions 中 live 状态的记录为 rolled_back（数据层，不删数据）。
"""

import sqlite3

from hippocampus.memory import feedback as fb


def count_feedback_stats(conn: sqlite3.Connection) -> dict:
    """统计 feedback_logs 中的事件计数。"""
    stats = {}
    for et in ("first_mention", "alarm", "correction", "diagnosis", "repair", "verify"):
        row = conn.execute("SELECT COUNT(*) FROM feedback_logs WHERE event_type=?", (et,)).fetchone()
        stats[et] = row[0] if row else 0
    return stats


def calculate_recall_rate(conn: sqlite3.Connection) -> float:
    """重述率 = alarm / first_mention（无 first_mention 则 0）。"""
    stats = count_feedback_stats(conn)
    if stats["first_mention"] == 0:
        return 0.0
    return stats["alarm"] / stats["first_mention"]


def calculate_confirm_response_rate(conn: sqlite3.Connection) -> float:
    """确认块响应率。

    proxy_server 中确认块通过 STATE.pending_block 追踪，
    响应（confirm/veto）会清空 pending_block。
    这里从 feedback_logs 的 extra 字段统计（如果在 proxy 中记录了）。
    简化实现：如果有 confirm/veto 相关日志就统计，否则返回 0。
    """
    # 尝试从 feedback_logs 的 extra 中找 confirm 相关事件
    rows = conn.execute(
        "SELECT extra FROM feedback_logs WHERE event_type='repair' AND extra LIKE '%confirm%'"
    ).fetchall()
    total_confirms = len(rows)
    if total_confirms == 0:
        return 0.0
    responded = sum(1 for r in rows if "responded" in (r["extra"] or ""))
    return responded / total_confirms if total_confirms > 0 else 0.0


def calculate_zero_hit_rate(conn: sqlite3.Connection) -> float:
    """检索零命中率。

    从 feedback_logs 的 diagnosis 事件中统计 locate='Q1'（零命中）的比例。
    """
    rows = conn.execute("SELECT locate FROM feedback_logs WHERE event_type='diagnosis'").fetchall()
    total = len(rows)
    if total == 0:
        return 0.0
    zero_hits = sum(1 for r in rows if r["locate"] == "Q1")
    return zero_hits / total


def generate_report(conn: sqlite3.Connection) -> dict:
    """生成观察期报告。"""
    stats = count_feedback_stats(conn)
    return {
        "signal_recurrence_rate": {
            "value": round(calculate_recall_rate(conn), 4),
            "formula": "alarm / first_mention",
            "alarm_count": stats["alarm"],
            "first_mention_count": stats["first_mention"],
        },
        "signal_confirm_response_rate": {
            "value": round(calculate_confirm_response_rate(conn), 4),
        },
        "signal_zero_hit_rate": {
            "value": round(calculate_zero_hit_rate(conn), 4),
        },
        "feedback_event_counts": stats,
    }


def rollback_param(conn: sqlite3.Connection, version_id: str, reason: str = "") -> bool:
    """回滚参数版本：标记为 rolled_back（数据层，不删数据）。"""
    row = conn.execute("SELECT gate_status FROM param_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return False
    # 只有 live 或 shadow 状态可以回滚
    if row["gate_status"] not in ("live", "shadow"):
        return False
    fb.update_param_gate(
        conn, version_id, "rolled_back", verify_result=f"rolled_back: {reason}" if reason else "rolled_back"
    )
    return True


def rollback_by_name(conn: sqlite3.Connection, param_name: str, reason: str = "") -> int:
    """按参数名回滚所有 live/shadow 版本，返回回滚数。"""
    versions = fb.get_param_versions(conn, param_name=param_name)
    count = 0
    for v in versions:
        if v["gate_status"] in ("live", "shadow"):
            rollback_param(conn, v["id"], reason)
            count += 1
    return count
