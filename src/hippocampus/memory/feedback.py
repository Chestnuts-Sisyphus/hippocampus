# 由前身 hippocampus_prototype/feedback.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 反馈环 -- 事件日志 + 参数版本管理（N1/N5）

feedback_logs: 记录反馈环全生命周期事件（first_mention→alarm→diagnosis→repair→verify）
param_versions: 参数变更五道闸门（pending→replay→shadow→live | rolled_back）

只依赖 database + sqlite3 + hashlib，零新增依赖。
"""

import hashlib
import json
import sqlite3

from hippocampus.memory import database as db

# ---------- 主题指纹 ----------


def topic_fingerprint(text: str) -> str:
    """对文本取 SHA256 前16位作为主题指纹——用于跨事件关联同一主题。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------- feedback_logs CRUD ----------


def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    topic_fp: str = "",
    locate: str = "",
    root_cause: str = "",
    attribution: str = "",
    fix: str = "",
    verify_result: str = "",
    prevent_result: str = "",
    extra: dict | None = None,
) -> str:
    """记录一条反馈事件，返回事件 ID。extra 存为 JSON 字符串。"""
    eid = db.gen_id("fb")
    conn.execute(
        "INSERT INTO feedback_logs "
        "(id, event_type, topic_fingerprint, locate, root_cause, attribution, "
        " fix, verify_result, prevent_result, extra, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid,
            event_type,
            topic_fp,
            locate,
            root_cause,
            attribution,
            fix,
            verify_result,
            prevent_result,
            json.dumps(extra or {}, ensure_ascii=False),
            db.now_ms(),
        ),
    )
    conn.commit()
    return eid


def query_events_by_topic(conn: sqlite3.Connection, topic_fp: str) -> list[dict]:
    """按主题指纹查询所有事件（按时间升序）。"""
    rows = conn.execute(
        "SELECT * FROM feedback_logs WHERE topic_fingerprint=? ORDER BY created_at ASC",
        (topic_fp,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_unclosed_first_mention(conn: sqlite3.Connection, topic_fp: str) -> dict | None:
    """查该主题是否有未闭环的 first_mention（即之后没有 alarm/diagnosis/repair/verify 关闭它）。
    简化判断：找最新的 first_mention，如果它之后没有 verify 事件则视为未闭环。
    """
    events = query_events_by_topic(conn, topic_fp)
    if not events:
        return None
    # 找最后一个 first_mention
    last_fm = None
    for e in events:
        if e["event_type"] == "first_mention":
            last_fm = e
    if last_fm is None:
        return None
    # 检查其后是否有 verify（闭环标记）
    last_fm_ts = last_fm["created_at"]
    for e in events:
        if e["created_at"] > last_fm_ts and e["event_type"] == "verify":
            return None  # 已闭环
    return last_fm


def update_event(conn: sqlite3.Connection, event_id: str, **fields) -> None:
    """更新事件的生命周期字段（只能更新七字段 + extra）。"""
    allowed = {"locate", "root_cause", "attribution", "fix", "verify_result", "prevent_result", "extra"}
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            if k == "extra":
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(db.now_ms())
    vals.append(event_id)
    conn.execute(
        f"UPDATE feedback_logs SET {', '.join(sets)}, "
        f"(SELECT 1) "  # no-op to keep updated_at pattern
        f"WHERE id=?",
        vals,
    )
    # 上面 SQL 有语法问题，用简单版
    conn.execute(
        f"UPDATE feedback_logs SET {', '.join(sets)} WHERE id=?",
        [v for v in fields.values() if True] + [event_id] if False else vals[:-1] + [event_id],
    )
    conn.commit()


# ---------- param_versions CRUD（N5） ----------


def create_param_version(
    conn: sqlite3.Connection,
    param_name: str,
    old_value: str,
    new_value: str,
    reason: str = "",
    gate_status: str = "pending",
    verify_result: str = "",
) -> str:
    """创建一条参数版本记录。"""
    vid = db.gen_id("pv")
    conn.execute(
        "INSERT INTO param_versions "
        "(id, param_name, old_value, new_value, reason, gate_status, verify_result, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (vid, param_name, old_value, new_value, reason, gate_status, verify_result, db.now_ms()),
    )
    conn.commit()
    return vid


def get_param_versions(conn: sqlite3.Connection, param_name: str = "", gate_status: str = "") -> list[dict]:
    """查询参数版本记录，可按名称和状态过滤。"""
    sql = "SELECT * FROM param_versions WHERE 1=1"
    params = []
    if param_name:
        sql += " AND param_name=?"
        params.append(param_name)
    if gate_status:
        sql += " AND gate_status=?"
        params.append(gate_status)
    sql += " ORDER BY created_at ASC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_param_gate(conn: sqlite3.Connection, version_id: str, gate_status: str, verify_result: str = "") -> None:
    """更新参数版本的闸门状态。"""
    conn.execute(
        "UPDATE param_versions SET gate_status=?, verify_result=? WHERE id=?",
        (gate_status, verify_result, version_id),
    )
    conn.commit()


def seed_default_params(conn: sqlite3.Connection) -> int:
    """预置 5 个候选参数（pending 状态，"估计值待校准"）。
    幂等：已存在同名 pending 记录则跳过。
    """
    defaults = [
        ("semantic_threshold", "0.1", "估计值待校准"),
        ("retrieval_top_k", "8", "估计值待校准"),
        ("hub_threshold", "30", "估计值待校准"),
        ("density_ratio", "1:10", "估计值待校准"),
        ("reextract_attempts", "3", "估计值待校准"),
    ]
    count = 0
    for name, old_val, reason in defaults:
        existing = get_param_versions(conn, param_name=name, gate_status="pending")
        if existing:
            continue
        create_param_version(conn, name, old_val, "", reason, "pending")
        count += 1
    return count


# ---------- 初始化 ----------


def ensure_feedback_tables(conn: sqlite3.Connection) -> None:
    """确保反馈环表已创建（connect() 已在 SCHEMA 中建表，此处做幂等保险）。"""
    # SCHEMA 中已有 CREATE TABLE IF NOT EXISTS，connect() 时自动执行
    # 此函数供独立调用时保险
    pass
