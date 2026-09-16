# 由前身 hippocampus_prototype/schema_compact.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus A3-3 —— 图式化/去重增强（schema_compact.py，HC-0815-02 节点6 新增）

多条语义相近的 preference 聚成一组 → 概括成一条更高层规则，旧条标 superseded
（不物理删除）。用现有语义去重阈值（dedup.SEMANTIC_DUP_THRESHOLD=0.85，中文
MiniLM 虚高防御同源）聚组；规则内容由 LLM 概括（单次 chat_json，失败跳过该组）。

- 候选：type='preference' AND status='active' AND shadow=0 的正式记忆
- 聚组：choma 近邻 sim ≥ 阈值两两连通并组（collections 为 None → 无法语义聚组，
  空跑返回零；聚组异常软失败）
- 组大小 ≥ 2 才合并；规则条继承组内实体并集 + source_quote 拼接
- 旧条用 db.supersede_memory（规则胜出方 SUPERSEDES 旧条，不物理删除）
- 绝不碰真实库：调用方负责传隔离连接
"""

import json
import sqlite3
import sys
from typing import Any

from hippocampus.memory import database as db
from hippocampus.memory import dedup
from hippocampus.memory import retrieval as rt

# 聚组最小规模：至少 2 条相似记忆才值得概括成规则
MIN_GROUP_SIZE = 2
# 单次最多处理候选数（防库增长调用爆炸）
_MAX_CANDIDATES = 100

_COMPACT_SYSTEM = """你是记忆概括器。把以下一组语义相近的用户偏好记忆概括成一条更高层的规则。
只输出紧凑JSON：{"rule": "一句话规则，涵盖所有条目的共同含义"}
不要编造组内没有的信息；不要逐条复述。"""


def _candidates(conn: sqlite3.Connection, limit: int = _MAX_CANDIDATES) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM memories WHERE type='preference' AND status='active' "
        "AND COALESCE(shadow,0)=0 ORDER BY created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _group_candidates(conn: sqlite3.Connection, collections: dict, cands: list[dict]) -> list[list[dict]]:
    """按语义相似聚组：对每条候选查近邻（sim ≥ 阈值），两两连通并组。
    近邻查询异常软失败（该条视为独立组不合并）。"""
    if not cands:
        return []
    groups: list[list[dict]] = []
    by_id = {c["id"]: c for c in cands}
    for c in cands:
        try:
            hits = rt.semantic_search(
                collections["mem"], c["content"], n=10, query_embedding=rt._query_embedding(c["content"])
            )
        except Exception as e:
            sys.stderr.write(f"[compact] 近邻查询失败（{c['id']} 独立）: {e}\n")
            hits = {}
        similar = {
            doc_id
            for doc_id, v in hits.items()
            if v.get("kind") == "memory"
            and v.get("sim", 0.0) >= dedup.SEMANTIC_DUP_THRESHOLD
            and doc_id in by_id
        }
        similar.add(c["id"])
        # 并查集式合并：与已有组有交集则并入
        merged = None
        for g in groups:
            if g and (g[0]["id"] in similar or any(m["id"] in similar for m in g)):
                merged = g
                break
        if merged is None:
            groups.append([c])
        else:
            for sid in similar:
                cand = by_id[sid]
                if cand["id"] not in {m["id"] for m in merged}:
                    merged.append(cand)
    return [g for g in groups if len(g) >= MIN_GROUP_SIZE]


def _summarize_rule(conn: sqlite3.Connection, group: list[dict]) -> str | None:
    """LLM 概括组内偏好为一条规则；失败/空返回 None（跳过该组不合并）。"""
    lines = [f"- {m['content']}" for m in group]
    try:
        from hippocampus.memory.llm import chat_json

        result = chat_json(_COMPACT_SYSTEM, "用户偏好记忆组：\n" + "\n".join(lines), max_tokens=300)
        rule = str(result.get("rule", "")).strip()
        return rule or None
    except Exception as e:
        sys.stderr.write(f"[compact] 规则概括失败（跳过该组）: {e}\n")
        return None


def compact_schemas(
    conn: sqlite3.Connection, collections: dict | None = None, now: int | None = None
) -> dict[str, Any]:
    """图式化主入口：语义相近的 preference 聚组 → 概括成一条规则，旧条 superseded。

    参数：
      conn: 账户隔离连接（调用方保证不传真实库）
      collections: 双池 dict {mem, ep}（语义聚组必需；None → 空跑返回零）
      now: 当前毫秒时间戳（测试注入用）；None = db.now_ms()

    返回 {"groups": 合并组数, "rules": 写入规则数, "superseded": 旧条数}。
    聚组/概括任何异常软失败，绝不阻断调用方。
    """
    if collections is None:
        return {"groups": 0, "rules": 0, "superseded": 0}
    try:
        cands = _candidates(conn)
        groups = _group_candidates(conn, collections, cands)
        rules = 0
        superseded = 0
        for group in groups:
            try:
                rule = _summarize_rule(conn, group)
                if not rule:
                    continue
                # 规则条：继承组内实体并集 + 组内容作 source_quote
                entity_ids: list[str] = []
                for m in group:
                    try:
                        for eid in json.loads(m["entity_ids"] or "[]"):
                            if eid not in entity_ids:
                                entity_ids.append(eid)
                    except (json.JSONDecodeError, TypeError):
                        continue
                quotes = "；".join((m["source_quote"] or m["content"])[:80] for m in group)
                winner = db.add_memory(
                    conn,
                    "preference",
                    rule,
                    entity_ids=entity_ids,
                    source_quote=quotes[:500],
                    scene_description="A3-3 图式化：多条相似偏好概括",
                )
                for m in group:
                    db.supersede_memory(conn, winner, m["id"])
                conn.commit()
                rules += 1
                superseded += len(group)
            except Exception as e:
                sys.stderr.write(f"[compact] 组合并失败（跳过）: {e}\n")
                continue
        return {"groups": len(groups), "rules": rules, "superseded": superseded}
    except Exception as e:
        sys.stderr.write(f"[compact] 图式化失败（软失败）: {e}\n")
        return {"groups": 0, "rules": 0, "superseded": 0}
