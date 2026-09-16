# 由前身 hippocampus_prototype/hub_guard.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus B2 —— mega-hub 三道防线之第三道（hub_guard.py）
对应设计文档 v2.1 第九节「mega-hub 三道防线」：
  1. 写入时关联到最具体的实体（提取 prompt 已实现）
  2. 检索时限制遍历：单实体最多取 20 条记忆（retrieval.py MAX_GRAPH_PER_ENTITY，勿改逻辑）
  3. 自动监控标记：ABOUT 关系超 30 条的实体标记为 hub（本模块），
     检查能否分流到子实体（输出 PART_OF/RELATED_TO 子实体清单）
标记落库：entities.is_hub=1（任务书拍板：entities 表 ALTER 加 is_hub 字段，默认 0）。
阈值是估计值可配置。只标记 + 出建议，不删改任何数据。
"""

import sqlite3

from hippocampus.memory import database as db

HUB_THRESHOLD = 30  # ABOUT 关系数阈值（>30 标记 hub，估计值可配置）


def count_about(conn: sqlite3.Connection, eid: str) -> int:
    """该实体的 active 记忆 ABOUT 关系数。
    （与检索负担口径一致：检索侧只查 active 记忆；superseded 记忆不构成当前 hub 负担）"""
    return conn.execute(
        "SELECT COUNT(*) FROM relations r JOIN memories m ON m.id = r.from_id "
        "WHERE r.rel_type='ABOUT' AND r.from_type='memory' AND r.to_type='entity' "
        "AND r.to_id=? AND m.status='active'",
        (eid,),
    ).fetchone()[0]


def list_sub_entities(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """分流建议：该实体 PART_OF/RELATED_TO 的直接邻居（子实体/关联实体）。
    返回 [{"id", "name", "rel"}, ...]（仅 active 实体）。"""
    nbrs = []
    rows = conn.execute(
        "SELECT from_id, to_id, rel_type FROM relations "
        "WHERE rel_type IN ('PART_OF','RELATED_TO') "
        "AND from_type='entity' AND to_type='entity' AND (from_id=? OR to_id=?)",
        (eid, eid),
    ).fetchall()
    for r in rows:
        other = r["to_id"] if r["from_id"] == eid else r["from_id"]
        ent = db.get_entity(conn, other)
        if ent and ent["status"] == "active":
            nbrs.append({"id": other, "name": ent["canonical_name"], "rel": r["rel_type"]})
    return nbrs


def scan_hubs(conn: sqlite3.Connection, verbose: bool = True) -> dict:
    """扫描 mega-hub：ABOUT 数 > 阈值 → entities.is_hub=1 + 输出分流建议。
    返回 {"hubs": [eid...], "suggestions": {eid: [子实体...]}}。只标记，不删改。"""
    rows = conn.execute("SELECT * FROM entities WHERE status='active'").fetchall()
    hubs, suggestions = [], {}
    for row in rows:
        n = count_about(conn, row["id"])
        if n <= HUB_THRESHOLD:
            continue
        if not row["is_hub"]:
            conn.execute("UPDATE entities SET is_hub=1, updated_at=? WHERE id=?", (db.now_ms(), row["id"]))
        hubs.append(row["id"])
        subs = list_sub_entities(conn, row["id"])
        suggestions[row["id"]] = subs
        if verbose:
            sub_line = (
                f"，可分流到子实体: {[s['name'] for s in subs]}" if subs else "，无子实体可分流（考虑建更具体的实体）"
            )
            print(f"  [mega-hub] 实体 '{row['canonical_name']}' ABOUT {n} 条 > {HUB_THRESHOLD} → is_hub=1{sub_line}")
    conn.commit()
    return {"hubs": hubs, "suggestions": suggestions}
