"""示例数据（`hippocampus seed`）——**公开仓里只有这一份数据，且是合成的**。

设计要点（验收 A32／§1.7 纪律）：
1. **已知真值**：每条记忆带"为什么在这"的用途标签（`role`），评测与标定脚本据此判定，
   不靠人工记忆；
2. 覆盖四类关键情形：普通事实、**冲突对**（同对象取值不同，应挂起确认）、
   **过期项**（生命周期演示）、**来源轨样本**（模型输出永不注入）；
3. **与真实数据无关**：主题取自公开的"求职偏好/工具偏好"通用场景，不含任何真实
   岗位池、公司名、个人信息；
4. 时间戳可注入（`as_of`），配合 `--now` 让生命周期/固化演示在同一代码路径上压缩时间。

用法：
    from hippocampus.seed import seed
    seed(core, Scope(account="demo"))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hippocampus.core import MemoryCore, Scope

DAY_MS = 86_400_000


@dataclass
class SeedItem:
    content: str
    kind: str
    role: str = "ordinary"          # ordinary | conflict_old | conflict_new | expired | model_track | fact
    quote: str = ""
    age_days: int = 0               # 距今多少天写入（0=今天）
    lifecycle: str = ""             # 演示用：直接置为 dormant / archived
    last_hit_age_days: int = -1     # 演示用：最后命中距今多少天（-1=不动）
    tags: list[str] = field(default_factory=list)


# 主题：一个虚构的"找实习/求职"场景（与真实岗位池无关）
SEED_ITEMS: list[SeedItem] = [
    # --- 普通偏好（注入会用到，评测的"应命中"真值） ---
    SeedItem("我只看允许远程的岗位", "preference", "ordinary", "只找远程的", age_days=30),
    SeedItem("我不投需要长期出差的岗位", "preference", "ordinary", "不想出差", age_days=28),
    SeedItem("简历投递前必须先过一遍错别字", "preference", "ordinary", "投之前检查错别字", age_days=21),
    SeedItem("技术面之前我要先做一遍同岗位真题", "preference", "ordinary", "面之前刷真题", age_days=14),
    # --- 事实 ---
    SeedItem("我的目标岗位方向是后端开发与基础设施", "fact", "fact", "方向是后端和基础设施", age_days=40),
    SeedItem("我每周三晚上固定留给项目开发", "fact", "fact", "周三晚上做项目", age_days=35),
    SeedItem("我用 Obsidian 管理笔记", "fact", "fact", "Obsidian 笔记", age_days=25),
    # --- 真实 JD 派生场景的答题要点（N8 新增；脱敏，不含私人 JD 原文） ---
    SeedItem("我优先投 Python 技术栈的公司", "preference", "ordinary", "Python 生态", age_days=20),
    SeedItem("我有记忆系统项目经验，会工具调用和多步任务", "fact", "fact", "agent 岗位答题要点", age_days=15),
    SeedItem("我做记忆系统：记忆要长期保存、模型输出不能污染记忆、检索要保证召回", "fact", "fact", "记忆系统要点", age_days=12),
    SeedItem("我做过向量检索与召回（记忆核心的语义通道）", "fact", "fact", "RAG 答题要点", age_days=10),
    SeedItem("记忆系统有生命周期管理让记忆过期，离线固化保存长期记忆", "fact", "fact", "不遗忘答题要点", age_days=8),
    # --- 冲突对：同对象取值不同（新值应挂起确认，老值在裁决前保持生效） ---
    SeedItem("我的期望城市是北京", "preference", "conflict_old", "想去北京", age_days=45),
    SeedItem("我的期望城市是杭州", "preference", "conflict_new", "改主意了想去杭州", age_days=2),
    # --- 过期项：构造时间使之降级/归档（A26／A31 演示） ---
    SeedItem("我在准备 2026 年春季的实习申请", "status", "expired", "春季实习申请", age_days=400),
    SeedItem("我最近在刷算法题，每天两道", "status", "expired", "每天两道题", age_days=500, lifecycle="dormant"),
    # --- 模型轨样本：来自模型输出，shadow=1，**永不注入** ---
    SeedItem("用户可能更偏好远程优先的团队", "fact", "model_track", "（模型归纳，未经用户确认）", age_days=5),
    SeedItem("用户似乎对通勤时间敏感", "fact", "model_track", "（模型归纳，未经用户确认）", age_days=5),
]

# 冲突对（seed 后供 demo 展示"挂起确认"用）
CONFLICT_PAIRS: list[tuple[str, str]] = [("我的期望城市是北京", "我的期望城市是杭州")]

# 评测题的真值映射：query → 应命中的记忆内容（子串）
KNOWN_RELEVANCE: dict[str, list[str]] = {
    "我投简历有什么要求": ["我只看允许远程的岗位", "简历投递前必须先过一遍错别字"],
    "面试之前要做什么准备": ["技术面之前我要先做一遍同岗位真题"],
    "我想去哪个城市工作": ["我的期望城市是杭州"],
    "我的岗位方向是什么": ["我的目标岗位方向是后端开发与基础设施"],
}

def seed(core: MemoryCore, scope: Scope, *, as_of_ms: int | None = None, verbose: bool = False) -> dict[str, Any]:
    """把示例记忆灌进指定 scope。返回统计（供 CLI 打印与评测引用）。

    `as_of_ms`：基准时间（None=当前时间）。写入时间回拨到 `age_days` 天前，
    使"过期/降级"这类需要真实时间跨度的情形在演示里可直接观察（A31）。
    """
    import time as _time


    now = int(as_of_ms if as_of_ms is not None else _time.time() * 1000)
    created: list[dict[str, Any]] = []
    for item in SEED_ITEMS:
        result = core.write(scope, item.content, kind=item.kind, source_quote=item.quote)
        if not result.ids:
            continue
        mid = result.ids[0]
        created.append({"id": mid, "role": item.role, "content": item.content, "kind": item.kind})
        session = core._session(scope)  # noqa: SLF001  # seed 是内部工具，与核心同仓
        with session.lock:
            created_at = now - item.age_days * DAY_MS
            session.conn.execute(
                "UPDATE memories SET created_at=?, updated_at=? WHERE id=?", (created_at, created_at, mid)
            )
            if item.last_hit_age_days >= 0:
                session.conn.execute(
                    "UPDATE memories SET last_hit_at=? WHERE id=?",
                    (now - item.last_hit_age_days * DAY_MS, mid),
                )
            elif item.role != "expired":
                session.conn.execute("UPDATE memories SET last_hit_at=? WHERE id=?", (created_at, mid))
            if item.lifecycle:
                session.conn.execute("UPDATE memories SET lifecycle=? WHERE id=?", (item.lifecycle, mid))
            if item.role == "model_track":
                session.conn.execute("UPDATE memories SET shadow=1 WHERE id=?", (mid,))
            session.conn.commit()
        if verbose:
            print(f"  [{item.kind}] {item.content}  ({item.role})")

    # 生命周期扫描：让"过期项"真的降级（同一代码路径，时间参数注入）
    try:
        from hippocampus.memory import lifecycle

        session = core._session(scope)  # noqa: SLF001  # 同上：内部工具直接用会话
        with session.lock:
            lifecycle.scan_lifecycle(session.conn, verbose=False, now=now)
    except Exception as e:  # 软失败：seed 不该因为生命周期扫描失败而中断
        print(f"  [seed] 生命周期扫描跳过（软失败）: {e}")

    stats = core.stats(scope)
    return {
        "created": created,
        "count": len(created),
        "stats": stats,
        "conflict_pairs": CONFLICT_PAIRS,
        "known_relevance": KNOWN_RELEVANCE,
        "as_of_ms": now,
    }


__all__ = ["CONFLICT_PAIRS", "KNOWN_RELEVANCE", "SEED_ITEMS", "SeedItem", "seed", "DAY_MS"]
