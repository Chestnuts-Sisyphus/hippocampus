# 由前身 hippocampus_prototype/retrieval_guard.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus B2 —— 第二重漏提取检测：检索兜底（retrieval_guard.py）
对应设计文档 v2.1 第十二节「四重漏提取检测 · 第二重：检索兜底」：
- 检索时记录"语义命中但实体未命中"的经历：
  通道B（向量语义）命中了某条经历，但通道A（实体关联）没命中——
  该经历的 entity_ids 不含当前查询解析出的实体 → 大概率漏提取
- 对该经历补实体提取 + 补 MENTIONS 关系（复用 missed_extract 模式：
  retroactive 标记、同一经历最多补 3 次、只加关系不删关系）
- "异步"简化为轮末同步执行（原型数据量小，不阻塞用户回复）
复用 missed_extract 的 tracker 表与提取函数：补提取约束（最多 3 次 / 只加不删 /
retroactive+时间戳）是四重检测共享的同一套规则，attempts 合并计数。
"""

import json
import sqlite3

from hippocampus.memory import database as db
from hippocampus.memory import missed_extract as me  # 复用 extract_entities_only / _resolve_entity / tracker 表

MAX_ITEMS_PER_RUN = 3  # 每轮最多补提取条数（防单轮 LLM 调用爆炸，估计值可配置）


def _check_tracker(conn: sqlite3.Connection, pid: str) -> tuple[int, str]:
    """查询该经历在 tracker 中的补提取状态。返回 (attempts, status)。
    无记录 → (0, '')；no_entity_confirmed 表示第一重已认定"真无实体"，不再补。"""
    me._ensure_tracker(conn)
    row = conn.execute("SELECT attempts, status FROM missed_extract_tracker WHERE episode_id=?", (pid,)).fetchone()
    if row is None:
        return 0, ""
    return row["attempts"], row["status"]


def _backfill_episode(conn: sqlite3.Connection, pid: str, verbose: bool = True) -> bool:
    """对单条经历补实体提取 + 补 MENTIONS 关系（只加不删）。
    提取失败 → attempts+1（第 3 次仍失败标 no_entity_confirmed 跳过）。
    返回是否修复（补上了实体）。"""
    row = conn.execute("SELECT content FROM episodes WHERE id=?", (pid,)).fetchone()
    if row is None:
        return False
    content = row["content"]
    ents = me.extract_entities_only(content)
    ts = db.now_ms()
    attempts, _st = _check_tracker(conn, pid)

    if not ents:
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
                f"  [漏提取·第二重] 经历 {pid} 第 {attempts} 次仍无实体"
                + ("，标 no_entity_confirmed 跳过" if status == "no_entity_confirmed" else "")
            )
        return False

    new_ids = []
    for ent in ents:
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        eid = me._resolve_entity(conn, name, ent.get("type", "Abstract"), ent.get("aliases", []) or [])
        new_ids.append(eid)
    if not new_ids:
        return False
    old_ids = json.loads(conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()["entity_ids"])
    merged = list(dict.fromkeys(old_ids + new_ids))  # 只加不删
    conn.execute("UPDATE episodes SET entity_ids=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), pid))
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
        print(f"  [漏提取·第二重] 经历 {pid} 补实体 {len(new_ids)} 个：{names}（retroactive {ts}）")
    return True


def run_retrieval_guard(
    conn: sqlite3.Connection, query_entity_ids: list[str], channels: dict, verbose: bool = True
) -> list[str]:
    """第二重检索兜底（挂 cli_chat 轮末）。
    入参：retrieve() 返回的 channels（含 entity_ids 与 semantic_episodes）。
    触发条件（全部满足）：
      1. 查询解析出至少一个实体（否则无从判定"实体未命中"，避免误触发）
      2. 语义通道命中经历（通道B 命中）
      3. 该经历 entity_ids 与查询实体无交集（通道A 未命中）
      4. 该经历补提取次数 < 3（且未被 no_entity_confirmed 跳过）
    返回本次补提取的经历 id 列表。
    """
    me._ensure_tracker(conn)  # 表先建（即使无候选，查询方也能安全查 tracker）
    if not query_entity_ids:
        return []
    candidates = []
    for item in channels.get("semantic_episodes", []):
        pid = item["doc_id"]
        row = conn.execute("SELECT entity_ids FROM episodes WHERE id=?", (pid,)).fetchone()
        if row is None:
            continue
        ent_ids = json.loads(row["entity_ids"] or "[]")
        if any(e in query_entity_ids for e in ent_ids):
            continue  # 通道A 已命中，不算漏
        attempts, status = _check_tracker(conn, pid)
        if attempts >= 3 or status == "no_entity_confirmed":
            continue
        candidates.append(pid)
    if not candidates:
        return []
    fixed = []
    for pid in candidates[:MAX_ITEMS_PER_RUN]:
        if _backfill_episode(conn, pid, verbose=verbose):
            fixed.append(pid)
    return fixed
