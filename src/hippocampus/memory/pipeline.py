# 由前身 hippocampus_prototype/pipeline.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 原型 —— 提取→入库→查回 闭环（Day1 验收）
流程：LLM 提取实体+记忆 → 消歧第一层（按名匹配已有实体）→ 写入 SQLite → 按实体查回
"""

import json
import sqlite3
from typing import Any

from hippocampus.memory import database as db
from hippocampus.memory import dedup, event_time
from hippocampus.memory.extract import extract, is_emotional_status, is_transient_status

# 模块级锚点缓存：per-session 事件时间列表，每会话上限 20 条
_session_anchors: dict[str, list[int]] = {}
_MAX_ANCHORS = 20


def process_user_message(
    conn: sqlite3.Connection,
    session_id: str,
    text: str,
    memory_types: list[str] | None = None,
    episode_id: str | None = None,
    collections: dict | None = None,
) -> dict[str, Any]:
    """处理一条用户消息：提取 → 消歧 → 入库 → 返回摘要

    可选参数（轨道A前移用，默认 None 行为零变化）：
      memory_types: 只入库这些类型（如 ['preference','fact']）；None=全部类型。
      episode_id: 复用已建 episode（轨道A与 after_response 并发，避免 invalidation 前 status/resource
                  重复写库）。给出时跳过新建 episode，新记忆 source_episode_id 指向它。
      collections: 双池 dict {mem, ep}（P1 语义去重用；None=只做完全去重）。
    """
    # P0：提取前检测。E 组（PII）不调 extract；C 组交给 extract() 内部拦截（兼容 P04 mock）。
    _ep_flag = 0
    _skip_pii = False
    try:
        from hippocampus.memory import security as secmod

        _, _ep_flag, _skip_pii = secmod.precheck(text)
    except Exception:
        _ep_flag = 0
        _skip_pii = False

    if _skip_pii:
        result = {"entities": [], "memories": []}
    else:
        result = extract(text)

    # 1. 实体消歧第一层：名字匹配已有实体，匹配不到则新建
    entity_map: dict[str, str] = {}  # name -> entity_id
    for ent in result.get("entities", []):
        name = ent["name"].strip()
        if not name:
            continue
        existing = db.find_entity_by_name(conn, name)
        if existing:
            entity_map[name] = existing["id"]
            print(f"  [消歧] '{name}' → 命中已有实体 {existing['id']}")
        else:
            eid = db.add_entity(conn, name, ent.get("type", "Abstract"), aliases=ent.get("aliases", []))
            entity_map[name] = eid
            print(f"  [新建] '{name}' ({ent.get('type')}) → {eid}")

    # 2. 经历入库（用户原话，priority=high）
    # event_time 解析（开关在 pipeline 接入层生效，database 层只做语义归位）
    # 读活跃快照 event_time_enabled（参考 retrieval.py get_active_params 读法）
    event_time_val: int | None = None
    et_enabled = True  # 缺省开
    try:
        _row = conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
        if _row:
            _params = json.loads(_row["params"])
            et_enabled = _params.get("event_time_enabled", True)
    except Exception:
        pass
    if et_enabled:
        anchors = _session_anchors.get(session_id)
        event_time_val = event_time.parse_time(text, db.now_ms(), anchors)
        if event_time_val is not None:
            # 存锚点缓存（per-session，上限 _MAX_ANCHORS，超出丢最旧）
            buf = _session_anchors.setdefault(session_id, [])
            buf.append(event_time_val)
            if len(buf) > _MAX_ANCHORS:
                buf.pop(0)

    # P04 安全：episodes 原文同检同标（A/B/D 标记不拦截；flag 已在提取前算好）
    if episode_id is not None:
        # 复用已建 episode（并发防重复）
        pid = episode_id
    else:
        pid = db.add_episode(
            conn,
            session_id,
            "user",
            text,
            entity_ids=list(entity_map.values()),
            event_time=event_time_val,
            security_flag=_ep_flag,
        )

    # 3. 记忆入库（ADD-only：只加不改）
    memory_ids: list[str] = []
    for mem in result.get("memories", []):
        # 类型过滤（轨道A只取 preference/fact；after_response 只取 status/resource）
        if memory_types is not None and mem.get("type", "fact") not in memory_types:
            continue
        # P1（HC-0815-02 节点1）：瞬时状态机械拦截（HEAD 哈希/commit/run 号类，
        # 换 commit 即失效）→ 不入库。提取 prompt 约束是第一道，此处是兜底。
        if is_transient_status(mem):
            print(f"  [过滤] 瞬时状态不入库: {mem['content'][:40]}")
            continue
        # P2（HC-0815-02 节点2）：情绪宣泄机械拦截（「查 bug 三小时太烦了」→ 噪声）
        if is_emotional_status(mem):
            print(f"  [过滤] 情绪宣泄不入库: {mem['content'][:40]}")
            continue
        # P1：去重（HC-0901-01 分流：精确重复拦；语义命中不再静默丢——机械确认
        # 值变更→入库后取代旧值（SUPERSEDES），同构换值放行，其余保守拦+日志留痕
        # （宁冗余不丢变更）。语义层仅 preference（6c 收口边界不变）。
        # 同会话跨轮重复放行——tracka 验收语义：同一轮/同会话提取各入库一次）
        _mtype = mem.get("type", "fact")
        _supersede_old = None
        exact_dup = dedup.find_exact_duplicate(conn, _mtype, mem["content"], exclude_session_id=session_id)
        if exact_dup is not None:
            print(f"  [去重] 与 {exact_dup} 完全重复，跳过: {mem['content'][:40]}")
            continue
        if _mtype == "preference" and collections is not None:
            sem_dup = dedup.find_semantic_duplicate(
                conn, collections, _mtype, mem["content"], exclude_session_id=session_id
            )
            if sem_dup is not None:
                _old_row = conn.execute("SELECT content FROM memories WHERE id=?", (sem_dup,)).fetchone()
                _action = dedup.classify_dup_action(_old_row["content"] if _old_row else "", mem["content"])
                if _action == "supersede":
                    _supersede_old = sem_dup
                    print(f"  [去重] 语义命中且确认值变更，入库后取代 {sem_dup}: {mem['content'][:40]}")
                elif _action == "admit":
                    print(f"  [去重] 语义命中为同构换值，放行: {mem['content'][:40]}")
                else:
                    print(
                        f"  [去重] 语义命中按重复拦截 {sem_dup}: {mem['content'][:40]}"
                        "（若为变更误拦，观察日志回流 HC-0901-01）"
                    )
                    continue
        mem_entities = [entity_map[n] for n in mem.get("entity_names", []) if n in entity_map]
        # 记忆关联到最具体的实体（设计：防 mega-hub）——MVP 先全关联，后续优化
        # P04 安全：写入侧检测 content + source_quote；preference 跳过
        # （偏好管道零改动——确认机制天然安全，路线书第 8 条）
        _mem_flag = 0
        if mem.get("type", "fact") != "preference":
            try:
                from hippocampus.memory import security as secmod

                _mem_hits = secmod.check_text(mem["content"]) + secmod.check_text(mem.get("source_quote", ""))
                # 纵深：已提取出的 PII（E 组）丢弃不入库；C 组仍标记写入（P04 S2/S3）
                if any(h.get("group") == "E" for h in _mem_hits):
                    continue
                _mem_flag = secmod.max_flag(_mem_hits)
            except Exception:
                _mem_flag = 0
        mid = db.add_memory(
            conn,
            mem.get("type", "fact"),
            mem["content"],
            entity_ids=mem_entities,
            source_quote=mem.get("source_quote", ""),
            source_episode_id=pid,
            security_flag=_mem_flag,
        )
        if _supersede_old is not None:
            db.supersede_memory(conn, mid, _supersede_old, source_episode_id=pid)
            print(f"  [取代] {mid} SUPERSEDES {_supersede_old}（旧值降权不注入，数据不删）")
        memory_ids.append(mid)
        print(f"  [记忆] ({mem.get('type')}) {mem['content'][:40]}... → {mid}")

    conn.commit()
    return {"episode_id": pid, "entity_map": entity_map, "memory_ids": memory_ids}
