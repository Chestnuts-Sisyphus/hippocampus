# 由前身 hippocampus_prototype/conflict.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 原型 —— 冲突标注（Day2）
对应设计文档 v2.1 第六节步骤5（借鉴 Ghost Memory）：
  - 扫描同实体下的多条记忆，检测语义冲突（轻量 LLM 判断）
  - 冲突记忆按 created_at 排序：新的标 current，旧的标 historical
  - superseded 不注入（数据库层已过滤）
  - 都是 active 但检测到冲突的：都注入，但标注时间标签

MVP 说明：不修改数据库（ADD-only 原则），只在检索时动态标注，
标注结果用于注入格式（[类型·current/historical]）。
"""

import sqlite3

from hippocampus.memory.llm import chat_json

CONFLICT_SYSTEM = """你是记忆冲突检测器。判断一组记忆之间是否存在"真矛盾"。
规则：
- 真矛盾：同一主题，两个说法不能同时成立（如"设计已完成" vs "设计已推翻"）
- 不是矛盾：分场景的共存（如"喜欢简洁回复" vs "技术讨论时喜欢详细解释"）
- 不是矛盾：只是补充信息、角度不同（如"喜欢简洁" vs "厌恶打补丁式设计"）
只输出紧凑JSON：{"conflicts":[{"a":"记忆id","b":"记忆id","reason":"一句话理由"}]}
没有矛盾就输出空数组。"""


def detect_conflicts(conn: sqlite3.Connection, memories: list[dict]) -> list[tuple[str, str, str]]:
    """LLM 检测记忆列表中的矛盾对，返回 [(a_id, b_id, reason), ...]"""
    if len(memories) < 2:
        return []
    lines = []
    for m in memories:
        lines.append(f"[{m['id']}] ({m['type']}) {m['content']}")
    user = "以下记忆来自同一个人，判断是否有真矛盾：\n" + "\n".join(lines)
    try:
        r = chat_json(CONFLICT_SYSTEM, user, max_tokens=500)
        out = []
        for c in r.get("conflicts", []):
            a, b = str(c.get("a", "")), str(c.get("b", ""))
            if a and b and a != b:
                out.append((a, b, str(c.get("reason", ""))[:80]))
        return out
    except Exception as e:
        print(f"  [冲突检测调用失败] {e}")
        return []


# 批量化候选集上限：active 记忆最多取 30 条/批（防库增长调用爆炸，
# 任务书 8c 任务 1；现状 import_runner 是同类型全查，超限后仅影响超大库候选集）
_BATCH_CANDIDATE_LIMIT = 30


def batch_detect_conflicts(conn: sqlite3.Connection, new_mems: list[dict]) -> dict:
    """批量化冲突检测（任务书 8c 任务 1，C 病态修复）。

    把同消息提取的 N 条新记忆 + 候选集（active 已有记忆，去重后上限 30 条）
    一次性 detect_conflicts（单次 LLM，内部多对多判断）；
    返回 {memory_id: [(conflict_id, reason), ...]}（key 只含新记忆）；
    new_mems 为空返回 {}。
    单条新记忆时与旧路径等价（旧路径=逐条查同类型 active + 一次 detect_conflicts），
    仅候选集多了一条 30 条上限（任务书授权，防爆炸）。

    P2（HC-0815-02 节点2）：候选集从「按类型分别限量」改为「全类型统一限量」——
    技术栈矛盾（fact「用 SQLite」 vs resource「用 PostgreSQL」）是跨类型冲突，
    原按类型分批永远不同批（SUPERSEDES 盲区，观察线实测：并存 active 未触发）。
    """
    if not new_mems:
        return {}
    new_ids = [m["id"] for m in new_mems]
    new_id_set = set(new_ids)
    # 候选集：全部新记忆 + active 已有记忆（去重、全类型统一限量 30）
    cand = {m["id"]: m for m in new_mems}
    placeholders = ",".join("?" * len(new_ids))
    rows = conn.execute(
        "SELECT id, type, content, created_at FROM memories "
        f"WHERE status='active' AND id NOT IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT ?",
        new_ids + [_BATCH_CANDIDATE_LIMIT],
    ).fetchall()
    for r in rows:
        if r["id"] not in cand:
            cand[r["id"]] = dict(r)
    conflicts = detect_conflicts(conn, list(cand.values()))
    out = {}
    for a_id, b_id, reason in conflicts:
        if a_id in new_id_set:
            out.setdefault(a_id, []).append((b_id, reason))
        elif b_id in new_id_set:
            out.setdefault(b_id, []).append((a_id, reason))
    return out


def annotate(conn: sqlite3.Connection, results: list[dict]) -> dict:
    """对检索结果中的记忆做冲突标注，返回 {doc_id: 'current'|'historical'}
    只标注存在矛盾的记忆；无矛盾的不标注（不注入标签）。
    """
    mems = [r for r in results if r["kind"] == "memory"]
    if not mems:
        return {}
    ids = [r["doc_id"] for r in mems]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, type, content, created_at FROM memories WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    mem_list = [by_id[i] for i in ids if i in by_id]

    conflicts = detect_conflicts(conn, mem_list)
    tags = {}
    for a, b, reason in conflicts:
        if a not in by_id or b not in by_id:
            continue
        # created_at 新的标 current，旧的标 historical
        if by_id[a]["created_at"] >= by_id[b]["created_at"]:
            tags[a], tags[b] = "current", "historical"
        else:
            tags[b], tags[a] = "current", "historical"
        print(f"  [冲突] {by_id[a]['content'][:25]}... ↔ {by_id[b]['content'][:25]}...（{reason}）")
    return tags


# ---------------------------------------------------------------------------
# [HIPPO] 离线规则冲突检测（验收 A36／A30-③）
# ---------------------------------------------------------------------------
# 前身的冲突判定是**纯 LLM**（detect_conflicts → chat_json）。离线档没有模型，
# 于是"改口 → 挂起确认"这条链在无 key 环境下会断——而它正是本项目的招牌机制之一。
# 这里补一条**机械判据**（零 LLM、确定性），只认两种确凿情形：
#   ① 公共前缀占短串一半以上、且差异都在尾部 → "同对象的取值变了"（北京→杭州）
#   ② 极性／数值翻转（去否定字后相等、数值集合不同）→ 复用 dedup 的机械分流
# 其余一律**不判冲突**（宁可不问，不许乱问）。


def _common_prefix_len(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def is_same_subject_value_change(old: str, new: str) -> bool:
    """两条陈述是不是"同一个对象、取值不同"。

    两条判据，**先严后宽**，且第二条要先过相似度门——否则"我在准备 2026 年春季的
    实习申请"与"我最近在刷算法题，每天两道"这种**同含数字但主题不同**的句子会被
    机械分流误判成"值变更"（实测踩过：seed 里这两条被凑成了一对假冲突）。
    """
    from difflib import SequenceMatcher

    from hippocampus.memory import dedup

    norm_old = dedup.normalize_content(old)
    norm_new = dedup.normalize_content(new)
    if not norm_old or not norm_new or norm_old == norm_new:
        return False
    short = min(len(norm_old), len(norm_new))
    if short >= 4:
        prefix = _common_prefix_len(norm_old, norm_new)
        if prefix >= 0.5 * short and norm_old[prefix:] != norm_new[prefix:]:
            return True  # ① 同前缀、差异在尾部：明确是同对象的取值变了
    if SequenceMatcher(None, norm_old, norm_new).ratio() < 0.6:
        return False  # 门：主题都不像，后面的机械分流不适用
    try:
        return dedup.classify_dup_action(old, new) == "supersede"  # ② 极性/同值段翻转
    except Exception:
        return False


def detect_rule_conflicts(
    conn: sqlite3.Connection, content: str, mtype: str, *, limit: int = 20
) -> list[tuple[str, str, str]]:
    """规则冲突检测，返回 [(旧记忆 id, 占位 '', 理由)]（新条尚未入库，故用空串占位）。

    只看同类型、active、正式轨（shadow=0）的记忆，最多比 limit 条（按最近写入优先）。
    """
    if not content:
        return []
    try:
        rows = conn.execute(
            "SELECT id, content FROM memories "
            "WHERE type=? AND status='active' AND COALESCE(shadow,0)=0 "
            "ORDER BY created_at DESC LIMIT ?",
            (mtype, int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[tuple[str, str, str]] = []
    for row in rows:
        if is_same_subject_value_change(row["content"] or "", content):
            out.append((row["id"], "", "同对象取值不同（规则判定）"))
    return out


__all__ = ["detect_rule_conflicts", "is_same_subject_value_change"]
