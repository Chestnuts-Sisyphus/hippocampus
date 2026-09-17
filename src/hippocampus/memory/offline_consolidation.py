# 由前身 hippocampus_prototype/offline_consolidation.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus A3-2 —— 离线巩固（offline_consolidation.py，HC-0815-02 节点6 新增）

周期后台把超过 CONSOLIDATE_AFTER_MS（默认 7 天）且尚未蒸馏的 user episode
提炼成 memories（认知科学生命周期：经历 → 记忆）：

- 走现有 pipeline.process_user_message（复用实体消歧/event_time/瞬时拦截/情绪拦截/
  去重/类型过滤全套，episode_id 复用不建新 episode）
- 遵守学习开关（节点5）：learning_enabled=false 时整函数空跑（自动学习闸一致）
- 写回 episode.derived_memory_ids（JSON 数组），不删 episode（ADD 不 DELETE 铁律）
- 软失败：单条 episode 异常跳过继续，记 stderr；collections 为 None 时只做完全去重
- 绝不碰真实库：调用方负责传隔离连接
"""

import json
import sqlite3
import sys
from typing import Any

from hippocampus.memory import database as db
from hippocampus.memory import pipeline
from hippocampus.memory import retrieval as rt

# 默认巩固阈值：episode 超过 7 天未蒸馏才处理（新 episode 不碰）
CONSOLIDATE_AFTER_MS = 7 * 24 * 3600 * 1000


def _undistilled_episodes(conn: sqlite3.Connection, cutoff: int, limit: int = 200) -> list[dict]:
    """未蒸馏的 user episode 候选：derived_memory_ids 为空 且 timestamp ≤ cutoff。
    role='user' 才提炼（assistant 经历走观察轨独立路径）。"""
    rows = conn.execute(
        "SELECT * FROM episodes WHERE role='user' AND timestamp <= ? "
        "AND (derived_memory_ids IS NULL OR derived_memory_ids='' OR derived_memory_ids='[]') "
        "ORDER BY timestamp ASC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def run_offline_consolidation(
    conn: sqlite3.Connection, collections: dict | None = None, now: int | None = None, limit: int = 200
) -> dict[str, Any]:
    """离线巩固主入口：把超龄未蒸馏的 user episode 提炼成 memories。

    参数：
      conn: 账户隔离连接（调用方保证不传真实库）
      collections: 双池 dict {mem, ep}（语义去重用；None 时只做完全去重）
      now: 当前毫秒时间戳（测试注入用）；None = db.now_ms()
      limit: 单次处理 episode 上限（防一次跑爆）

    返回 {"episodes": 处理条数, "memories": 新记忆数, "skipped": 跳过条数}。
    学习开关 off（learning_enabled=false）→ 整函数空跑返回 {"episodes":0,"memories":0,"skipped":0}。
    任何单条异常软失败（记 stderr 继续），不阻断整批。
    """
    if now is None:
        now = db.now_ms()
    try:
        # 学习开关闸（节点5 同源语义：自动学习可被用户关停）
        if not rt.get_active_params(conn).get("learning_enabled", True):
            return {"episodes": 0, "memories": 0, "skipped": 0}
    except Exception as e:
        sys.stderr.write(f"[consolidation] 学习开关读取失败（按开启处理）: {e}\n")

    cutoff = now - CONSOLIDATE_AFTER_MS
    episodes = _undistilled_episodes(conn, cutoff, limit)
    total_memories = 0
    skipped = 0
    for ep in episodes:
        try:
            summ = pipeline.process_user_message(
                conn,
                ep["session_id"],
                ep["content"],
                episode_id=ep["id"],
                collections=collections,
            )
            mids = summ["memory_ids"]
            # 已蒸馏标记写回（含 0 条也标记，防重复尝试）
            conn.execute(
                "UPDATE episodes SET derived_memory_ids=? WHERE id=?",
                (json.dumps(mids, ensure_ascii=False), ep["id"]),
            )
            conn.commit()
            total_memories += len(mids)
            if not mids:
                skipped += 1
        except Exception as e:
            sys.stderr.write(f"[consolidation] episode {ep['id']} 巩固失败（跳过）: {e}\n")
            continue
    return {"episodes": len(episodes), "memories": total_memories, "skipped": skipped}
