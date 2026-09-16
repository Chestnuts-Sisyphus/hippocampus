# 由前身 hippocampus_prototype/lifecycle.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus B2 —— 容量分级管理（lifecycle.py）
对应设计文档 v2.1 第十节「容量分级管理」：

| 状态 | 条件 | 自动注入 | 回忆工具 | 显式查询 |
| active | 默认 | 是 | 是 | 是 |
| dormant | active 超 5000 条 或 6 个月未命中 | 否 | 是 | 是 |
| archived | dormant 持续 2 年未命中 | 否 | 否 | 是 |

- 与 status（active/superseded）正交：lifecycle 只管"活跃度"，不碰正确性
- 未命中用 last_hit_at 字段（默认 0 按 created_at 兜底）
- 阈值（5000 条 / 6 个月 / 2 年）是估计值，做成可配置常量
- 不删除任何记忆：只是越久远的越不容易自动浮出来
注入/回忆过滤在 cli_chat._filter_active（allow_dormant）实现；
命中更新在 cli_chat._retrieve_inject（db.touch_memory：dormant 命中复活为 active）。
本模块提供：显式扫描函数 scan_lifecycle（可手动触发转换）+ 显式查询辅助。
"""

import sqlite3

from hippocampus.memory import database as db

ACTIVE_MAX = 5000  # active 记忆总量上限（估计值可配置）
DORMANT_AFTER_MS = 6 * 30 * 24 * 3600 * 1000  # 6 个月未命中 → dormant
ARCHIVE_AFTER_MS = 2 * 365 * 24 * 3600 * 1000  # dormant 持续 2 年未命中 → archived
# P1（HC-0815-02 节点1）：瞬时状态（HEAD 哈希/commit/run 号类）超龄即降级
TRANSIENT_DEGRADE_AFTER_MS = 7 * 24 * 3600 * 1000  # 7 天（换 commit 即失效）


def _last_used_at(row) -> int:
    """最后命中时间：last_hit_at 默认 0 时按 created_at 兜底（从未命中）。"""
    return row["last_hit_at"] if row["last_hit_at"] else row["created_at"]


def degrade_transient(conn: sqlite3.Connection, now: int | None = None, max_age_ms: int = TRANSIENT_DEGRADE_AFTER_MS) -> int:
    """瞬时状态降级（P1 节点1）：type=status 且内容/来源匹配瞬时模式（HEAD 哈希/commit/run 号等，
    模式与 extract.is_transient_status 同源）且已存在超过 max_age_ms 的记忆 → 转 dormant。

    对提取约束生效前已入库的历史瞬时状态做定时失效兜底：dormant 不自动注入、
    可显式查询/回忆，不删除任何数据。返回降级条数。

    打回补丁（08-16）：只处理 type=status，与 is_transient_status 对齐——fact/preference/
    resource 即使文案含 HEAD/commit/任务描述，也不走这条 7 天降级
    （实测误伤：fact「HTTP HEAD 方法不带 body」被一次扫描降级）。
    """
    from hippocampus.memory.extract import TRANSIENT_STATUS_RE

    if now is None:
        now = db.now_ms()
    rows = conn.execute(
        "SELECT id, content, source_quote, created_at FROM memories "
        "WHERE lifecycle='active' AND type='status'"
    ).fetchall()
    n = 0
    for row in rows:
        text = f"{row['content'] or ''} {row['source_quote'] or ''}"
        if not TRANSIENT_STATUS_RE.search(text):
            continue
        if now - row["created_at"] <= max_age_ms:
            continue
        conn.execute("UPDATE memories SET lifecycle='dormant', updated_at=? WHERE id=?", (now, row["id"]))
        n += 1
    if n:
        conn.commit()
    return n


def scan_lifecycle(conn: sqlite3.Connection, verbose: bool = True, now: int | None = None) -> dict:
    """容量分级显式扫描（可手动触发）：执行三条规则，只改 lifecycle 不删任何数据。
    规则1：active 单条超 6 个月未命中 → dormant
    规则2：active 总量超 5000 条 → 最久未命中的先转 dormant，直到 ≤5000
    规则3：dormant 持续 2 年未命中 → archived
    返回 {"to_dormant": n, "to_archived": n, "total_active": n}"""
    now = now if now is not None else db.now_ms()
    to_dormant = 0
    to_archived = 0

    # 规则0（P1）：瞬时状态定时失效（HEAD 哈希/commit/run 号类，先于规则1/3，
    # 保证老库历史瞬时样本一次扫描即降级）
    transient_n = degrade_transient(conn, now)
    if verbose and transient_n:
        print(f"  [lifecycle] 瞬时状态超龄 → dormant {transient_n} 条")
    to_dormant += transient_n

    # 规则3：dormant → archived（先做，避免刚转 dormant 的记忆被立刻归档）
    rows = conn.execute("SELECT * FROM memories WHERE lifecycle='dormant' AND status='active'").fetchall()
    for row in rows:
        if now - _last_used_at(row) > ARCHIVE_AFTER_MS:
            conn.execute("UPDATE memories SET lifecycle='archived', updated_at=? WHERE id=?", (now, row["id"]))
            to_archived += 1
            if verbose:
                print(f"  [lifecycle] {row['id']} 持续 2 年未命中 → archived")

    # 规则1：active 单条超 6 个月未命中 → dormant
    rows = conn.execute("SELECT * FROM memories WHERE lifecycle='active' AND status='active'").fetchall()
    for row in rows:
        if now - _last_used_at(row) > DORMANT_AFTER_MS:
            conn.execute("UPDATE memories SET lifecycle='dormant', updated_at=? WHERE id=?", (now, row["id"]))
            to_dormant += 1
            if verbose:
                print(f"  [lifecycle] {row['id']} 超 6 个月未命中 → dormant")

    # 规则2：active 总量超上限 → 最久未命中的先转 dormant
    total_active = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE lifecycle='active' AND status='active'"
    ).fetchone()[0]
    over = total_active - ACTIVE_MAX
    if over > 0:
        rows = conn.execute(
            "SELECT id FROM memories WHERE lifecycle='active' AND status='active' "
            "ORDER BY (CASE WHEN last_hit_at=0 THEN created_at ELSE last_hit_at END) "
            "LIMIT ?",
            (over,),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE memories SET lifecycle='dormant', updated_at=? WHERE id=?", (now, r["id"]))
            to_dormant += 1
        if verbose:
            print(f"  [lifecycle] active 总量超 {ACTIVE_MAX} 条 → 转 dormant {len(rows)} 条")

    conn.commit()
    return {"to_dormant": to_dormant, "to_archived": to_archived, "total_active": total_active - max(over, 0)}


def query_explicit(conn: sqlite3.Connection, memory_id: str) -> dict | None:
    """显式查询辅助：按 id 查记忆，不受 lifecycle 限制（archived 也查得到）。
    对应设计文档"archived 仅显式查询"——数据库不隐藏任何记忆，此处只是显式入口。"""
    row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    return dict(row) if row else None


def list_all_lifecycles(conn: sqlite3.Connection) -> dict:
    """统计各 lifecycle 状态数量（运维/观察用）。"""
    out = {}
    for r in conn.execute("SELECT lifecycle, COUNT(*) AS n FROM memories GROUP BY lifecycle").fetchall():
        out[r["lifecycle"]] = r["n"]
    return out
