# 由前身 hippocampus_prototype/disambiguate.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 原型 —— 消歧第二层：异步聚类合并（问题 #2 验证）
设计文档 v2.1：定期扫描待消歧实体，LLM 判断语义相似的合并为一组
MVP 简版：对库里所有 active 实体做两两语义相似度检查（LLM 判定），相似→合并
合并：保留规范名更常用的实体为主实体，其他实体别名并入，关系转移，被合并实体标 merged
"""

import json
import sqlite3

from hippocampus.memory import database as db
from hippocampus.memory.llm import chat_json

DISAMBIG_SYSTEM = """你是实体消歧引擎。判断两个实体名是否指代同一个东西。
规则：
- 不同语言/不同叫法的同一事物（如"Hippocampus"和"海马体"）→ 同一实体
- 缩写/全称（如"LLM"和"大语言模型"）→ 同一实体
- 只是语义相关但确实不同（如"存储结构"和"检索策略"）→ 不同实体
只输出紧凑JSON：{"same": true或false, "reason": "一句话理由"}"""


def check_same(name1: str, name2: str) -> tuple[bool, str]:
    """LLM 判定两个实体名是否同一实体，返回 (是否同一, 理由)"""
    user = f"实体1: {name1}\n实体2: {name2}\n它们是同一个东西吗？"
    try:
        r = chat_json(DISAMBIG_SYSTEM, user, max_tokens=300)
        return bool(r.get("same")), str(r.get("reason", ""))[:80]
    except Exception as e:
        print(f"  [消歧调用失败] {name1} vs {name2}: {e}")
        return False, "调用失败"


def merge_entities(conn: sqlite3.Connection, keep_id: str, drop_id: str, reason: str) -> None:
    """把 drop 实体合并进 keep 实体：别名并入、关系转移、drop 标记 merged"""
    keep = db.get_entity(conn, keep_id)
    drop = db.get_entity(conn, drop_id)
    if not keep or not drop:
        return
    # 1. 别名并入（去重）
    aliases = json.loads(keep["aliases"]) if keep["aliases"] else []
    for a in [drop["canonical_name"]] + (json.loads(drop["aliases"]) if drop["aliases"] else []):
        if a not in aliases and a != keep["canonical_name"]:
            aliases.append(a)
    conn.execute(
        "UPDATE entities SET aliases=?, updated_at=? WHERE id=?",
        (json.dumps(aliases, ensure_ascii=False), db.now_ms(), keep_id),
    )
    # 2. 关系转移：drop 的所有关系改指向 keep
    conn.execute("UPDATE relations SET from_id=? WHERE from_id=? AND from_type='entity'", (keep_id, drop_id))
    conn.execute("UPDATE relations SET to_id=? WHERE to_id=? AND to_type='entity'", (keep_id, drop_id))
    # 3. 记忆/经历里的 entity_ids 数组替换
    for tbl in ("memories", "episodes"):
        rows = conn.execute(f"SELECT id, entity_ids FROM {tbl} WHERE entity_ids LIKE ?", (f'%"{drop_id}"%',)).fetchall()
        for row in rows:
            ids = json.loads(row["entity_ids"])
            ids = [keep_id if x == drop_id else x for x in ids]
            # 去重
            ids = list(dict.fromkeys(ids))
            conn.execute(f"UPDATE {tbl} SET entity_ids=? WHERE id=?", (json.dumps(ids, ensure_ascii=False), row["id"]))
    # 4. drop 标记 merged
    conn.execute("UPDATE entities SET status='merged', updated_at=? WHERE id=?", (db.now_ms(), drop_id))
    print(f"  [合并] '{drop['canonical_name']}' → '{keep['canonical_name']}'（{reason}）")


def run_disambiguation(conn: sqlite3.Connection, max_pairs: int = 10) -> dict:
    """扫描所有 active 实体，两两检查，合并相似对"""
    rows = conn.execute("SELECT * FROM entities WHERE status='active' ORDER BY created_at").fetchall()
    entities = [dict(r) for r in rows]
    merged = 0
    checked = 0
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            e1, e2 = entities[i], entities[j]
            if e1["status"] != "active" or e2["status"] != "active":
                continue
            if checked >= max_pairs:
                break
            checked += 1
            same, reason = check_same(e1["canonical_name"], e2["canonical_name"])
            if same:
                # 保留先创建的（更早的）为主实体
                merge_entities(conn, e1["id"], e2["id"], reason)
                merged += 1
        if checked >= max_pairs:
            break
    conn.commit()
    return {"checked_pairs": checked, "merged": merged}
