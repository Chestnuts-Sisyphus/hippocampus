# 由前身 hippocampus_prototype/missed_extract.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus B1 —— 第一重漏提取检测（missed_extract.py）
对应设计文档 v2.1 第十二节「四重漏提取检测 · 第一重：孤立扫描」：
- 扫描 entity_ids 为空的经历（孤立经历，大概率漏提取）
- 单独跑一次实体提取（只提实体，不做记忆提炼，prompt 更聚焦成本更低）
- 补 MENTIONS 关系（只加关系，不删已有关联；补提取记录来源 retroactive+时间戳）
补提取约束：
- 同一条经历最多补 3 次；第 3 次仍提取不到实体 → 标 no_entity_confirmed 跳过（极少数真无实体，如纯寒暄）
跟踪表建在本模块内部（不动 database.py 的 SCHEMA，符合 B1 白名单"database.py 只加函数"）。
"""

import json
import sqlite3

from hippocampus.memory import database as db
from hippocampus.memory.llm import chat_json

TRACKER_DDL = """
CREATE TABLE IF NOT EXISTS missed_extract_tracker (
    episode_id       TEXT PRIMARY KEY,
    attempts         INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | fixed | no_entity_confirmed
    last_attempt_at  INTEGER NOT NULL,
    source           TEXT DEFAULT ''
)
"""

# 只提实体，不做记忆提炼（比完整提取更聚焦、更便宜）
ENTITY_ONLY_SYSTEM = """你是实体提取引擎。本次任务只提取实体，不做记忆提炼。
【实体判断标准】用户能否用"那个X"独立指代它？能→是实体。不能→是属性，不提取。
【实体名规范】实体名必须是核心名词，不带修饰语。"X的Y"结构必须拆成两个实体。
【穷举原则】When in doubt, extract——宁可多提取也不漏提取，去重交给下游。
【输出格式】只输出一个紧凑 JSON 对象（不要缩进、不要多余换行）：
{"entities":[{"name":"实体名","type":"Concrete|Abstract|Event","aliases":["别名"]}]}
没有就输出空数组。"""


def _ensure_tracker(conn: sqlite3.Connection):
    conn.execute(TRACKER_DDL)


def _resolve_entity(conn: sqlite3.Connection, name: str, ent_type: str, aliases: list[str]) -> str:
    """消歧第一层：名字匹配已有实体，匹配不到新建（与 pipeline 同规则）"""
    existing = db.find_entity_by_name(conn, name)
    if existing:
        return existing["id"]
    return db.add_entity(conn, name, ent_type or "Abstract", aliases=aliases or [])


def extract_entities_only(text: str) -> list[dict]:
    """单次实体提取（只提实体），失败返回空列表"""
    try:
        r = chat_json(ENTITY_ONLY_SYSTEM, text, max_tokens=1500)
        return r.get("entities", []) or []
    except Exception as e:
        print(f"  [漏提取·实体提取失败] {e}")
        return []


def scan_and_fix(conn: sqlite3.Connection, verbose: bool = True, max_episodes: int = 20) -> int:
    """第一重漏提取：扫描孤立经历（entity_ids 为空），补实体+MENTIONS 关系。
    返回本次处理的经历条数。同一条最多补 3 次，第 3 次仍空标 no_entity_confirmed。"""
    _ensure_tracker(conn)
    # 孤立经历：entity_ids 为空 JSON 数组，且未被标 fixed / no_entity_confirmed
    rows = conn.execute(
        "SELECT id, content FROM episodes WHERE entity_ids='[]' "
        "AND id NOT IN (SELECT episode_id FROM missed_extract_tracker "
        "               WHERE status IN ('fixed','no_entity_confirmed')) "
        "ORDER BY created_at LIMIT ?",
        (max_episodes,),
    ).fetchall()
    if not rows:
        return 0
    handled = 0
    for row in rows:
        pid, content = row["id"], row["content"]
        tr = conn.execute("SELECT attempts, status FROM missed_extract_tracker WHERE episode_id=?", (pid,)).fetchone()
        attempts = tr["attempts"] if tr else 0
        if attempts >= 3:  # 上限已到但状态异常（防御），直接标跳过
            conn.execute("UPDATE missed_extract_tracker SET status='no_entity_confirmed' WHERE episode_id=?", (pid,))
            conn.commit()
            continue
        handled += 1
        ents = extract_entities_only(content)
        ts = db.now_ms()
        if ents:
            # 补实体关联（只加不删）+ MENTIONS 关系
            new_ids = []
            for ent in ents:
                name = (ent.get("name") or "").strip()
                if not name:
                    continue
                eid = _resolve_entity(conn, name, ent.get("type", "Abstract"), ent.get("aliases", []) or [])
                new_ids.append(eid)
            if new_ids:
                old_ids = json.loads(
                    conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()["entity_ids"]
                )
                merged = list(dict.fromkeys(old_ids + new_ids))  # 只加不删
                conn.execute(
                    "UPDATE episodes SET entity_ids=?, created_at=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False), ts, pid),
                )
                for eid in new_ids:
                    db.add_relation(conn, "episode", pid, "entity", eid, "MENTIONS", pid)
                conn.execute(
                    "INSERT OR REPLACE INTO missed_extract_tracker "
                    "(episode_id, attempts, status, last_attempt_at, source) VALUES (?,?,?,?,?)",
                    (pid, attempts + 1, "fixed", ts, f"retroactive {ts}"),
                )
                conn.commit()
                if verbose:
                    names = [e.get("name") for e in ents if (e.get("name") or "").strip()]
                    print(f"  [漏提取] 经历 {pid} 补实体 {len(new_ids)} 个：{names}")
        else:
            # 没提取到实体：attempts+1；第 3 次仍空 → no_entity_confirmed 跳过
            attempts += 1
            status = "no_entity_confirmed" if attempts >= 3 else "pending"
            conn.execute(
                "INSERT OR REPLACE INTO missed_extract_tracker "
                "(episode_id, attempts, status, last_attempt_at, source) VALUES (?,?,?,?,?)",
                (pid, attempts, status, ts, f"retroactive {ts}"),
            )
            conn.commit()
            if verbose:
                print(
                    f"  [漏提取] 经历 {pid} 第 {attempts} 次仍无实体"
                    + ("，标 no_entity_confirmed 跳过" if status == "no_entity_confirmed" else "")
                )
    return handled
