# 由前身 hippocampus_prototype/database.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 原型骨架 —— 数据库层
SQLite 主库：实体 / 记忆 / 经历 / 关系 四张表（对应设计文档 v2.1）
向量库：Chroma（独立文件，通过 memory_id/episode_id 关联回主库）
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from hippocampus.memory import runtime

# [HIPPO] 去全局单例：前身是 `DB_PATH = runtime.data_root() / "hippocampus.db"`（导入即定死，
# 多 scope 并行会串库）。现在 DB_PATH 只作**兼容锚点**（测试可显式赋值），为 None 时按
# 当前数据根动态解析。
DB_PATH: Path | None = None


def default_db_path() -> Path:
    return runtime.data_root() / "hippocampus.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id             TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases        TEXT DEFAULT '[]',      -- JSON 数组
    entity_type    TEXT NOT NULL,          -- Concrete | Abstract | Event
    description    TEXT DEFAULT '',
    status         TEXT DEFAULT 'active',  -- active | merged
    is_hub         INTEGER DEFAULT 0,      -- B2: mega-hub 标记（ABOUT>30 条，见 hub_guard）
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id                 TEXT PRIMARY KEY,
    type               TEXT NOT NULL,      -- preference | fact | resource | status
    status             TEXT DEFAULT 'active', -- active | superseded
    lifecycle          TEXT DEFAULT 'active', -- B2: active | dormant | archived（与 status 正交）
    last_hit_at        INTEGER DEFAULT 0,  -- B2: 最后命中时间（0=从未命中，按 created_at 兜底）
    entity_ids         TEXT DEFAULT '[]',  -- JSON 数组（ABOUT 关系）
    scene_tags         TEXT DEFAULT '[]',  -- JSON 数组
    scene_description  TEXT DEFAULT '',
    content            TEXT NOT NULL,
    content_lemmatized TEXT DEFAULT '',    -- BM25 用
    source_quote       TEXT DEFAULT '',
    source_episode_id  TEXT DEFAULT '',
    change_context     TEXT DEFAULT '',
    security_flag      INTEGER DEFAULT 0, -- P04: 内容注入检测标记（0=正常 1=可疑 2=高危）
    shadow             INTEGER DEFAULT 0, -- 轨道B: 1=观察期（不污染正式检索） 0=正式
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    timestamp            INTEGER NOT NULL,
    role                 TEXT NOT NULL,    -- user | assistant | tool
    content              TEXT NOT NULL,
    priority             TEXT NOT NULL,    -- high | medium | low
    entity_ids           TEXT DEFAULT '[]',-- JSON 数组（MENTIONS 关系）
    derived_memory_ids   TEXT DEFAULT '[]',-- JSON 数组
    security_flag        INTEGER DEFAULT 0, -- P04: 内容注入检测标记（0=正常 1=可疑 2=高危）
    created_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_type        TEXT NOT NULL,        -- entity | memory | episode
    from_id          TEXT NOT NULL,
    to_type          TEXT NOT NULL,
    to_id            TEXT NOT NULL,
    rel_type         TEXT NOT NULL,        -- ABOUT | MENTIONS | DERIVED_FROM | SUPERSEDES | COEXISTS_WITH | PART_OF | RELATED_TO
    source_episode_id TEXT DEFAULT '',
    extracted_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to   ON relations(to_type, to_id);
CREATE INDEX IF NOT EXISTS idx_memories_ent   ON memories(entity_ids);
CREATE INDEX IF NOT EXISTS idx_episodes_ent   ON episodes(entity_ids);
CREATE INDEX IF NOT EXISTS idx_episodes_ts    ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_episodes_sess  ON episodes(session_id);

-- 反馈环 N1：反馈日志表
CREATE TABLE IF NOT EXISTS feedback_logs (
    id                TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,          -- first_mention|alarm|correction|diagnosis|repair|verify
    topic_fingerprint TEXT DEFAULT '',        -- 主题指纹（跨事件关联同一主题）
    locate            TEXT DEFAULT '',        -- 生命周期1：定位
    root_cause        TEXT DEFAULT '',        -- 生命周期2：根因
    attribution       TEXT DEFAULT '',        -- 生命周期3：归因
    fix               TEXT DEFAULT '',        -- 生命周期4：修复
    verify_result     TEXT DEFAULT '',        -- 生命周期5：验证
    prevent_result    TEXT DEFAULT '',        -- 生命周期6：防复发结果
    extra             TEXT DEFAULT '{}',      -- 生命周期7：附加信息(JSON)
    created_at        INTEGER NOT NULL
);

-- 反馈环 N1/N5：参数版本表
CREATE TABLE IF NOT EXISTS param_versions (
    id               TEXT PRIMARY KEY,
    param_name       TEXT NOT NULL,           -- 参数名
    old_value        TEXT DEFAULT '',         -- 旧值
    new_value        TEXT DEFAULT '',         -- 新值
    reason           TEXT DEFAULT '',         -- 变更原因
    gate_status      TEXT DEFAULT 'pending',  -- pending|replay|shadow|live|rolled_back
    verify_result    TEXT DEFAULT '',         -- 验证结果
    created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fb_topic  ON feedback_logs(topic_fingerprint);
CREATE INDEX IF NOT EXISTS idx_fb_type   ON feedback_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_pv_name   ON param_versions(param_name);
CREATE INDEX IF NOT EXISTS idx_pv_status ON param_versions(gate_status);

-- 第三批：参数版本快照表（整组参数打包管理）
CREATE TABLE IF NOT EXISTS param_snapshots (
    id            TEXT PRIMARY KEY,
    version       TEXT NOT NULL,             -- 版本号如 v1.0
    params        TEXT NOT NULL,             -- JSON：5参数完整快照
    change_desc   TEXT DEFAULT '',           -- 变更说明
    reason        TEXT DEFAULT '',           -- 变更原因
    source        TEXT DEFAULT 'manual',     -- manual | auto
    gate_status   TEXT DEFAULT 'live',       -- live | pending | replay | shadow
    is_active     INTEGER DEFAULT 0,         -- 1=当前活跃
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ps_active ON param_snapshots(is_active);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, ddl: str) -> bool:
    """幂等 ALTER：列已存在则跳过，返回是否本次新增。"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    return True


def ensure_b2_schema(conn: sqlite3.Connection) -> dict[str, bool]:
    """B2 字段幂等迁移（老库补列，新库建表已含）：
    entities.is_hub / memories.lifecycle / memories.last_hit_at / memories.shadow。"""
    added = {
        "is_hub": _ensure_column(conn, "entities", "is_hub", "is_hub INTEGER DEFAULT 0"),
        "lifecycle": _ensure_column(conn, "memories", "lifecycle", "lifecycle TEXT DEFAULT 'active'"),
        "last_hit_at": _ensure_column(conn, "memories", "last_hit_at", "last_hit_at INTEGER DEFAULT 0"),
        "shadow": _ensure_column(conn, "memories", "shadow", "shadow INTEGER DEFAULT 0"),
    }
    conn.commit()
    return added


def seed_initial_snapshot(conn: sqlite3.Connection) -> bool:
    """幂等：如无快照则插入 v1.0（5个默认参数JSON），is_active=1。
    返回是否本次新增。"""
    existing = conn.execute("SELECT COUNT(*) FROM param_snapshots").fetchone()[0]
    if existing > 0:
        return False
    default_params = json.dumps(
        {
            "semantic_threshold": 0.1,
            "retrieval_top_k": 8,
            "hub_threshold": 30,
            "density_ratio": "1:10",
            "reextract_attempts": 3,
            "event_time_enabled": True,
            # 任务书 2（检索侧）新参数：机制开关 + 断崖/预算/事件层
            "retrieval_cliff_enabled": True,
            "session_entity_fallback": True,
            "shadow_log_enabled": True,
            "event_clue_limit": 200,
            "absolute_floor": 0.3,
            "cliff_gap_min": 0.1,
            "cliff_ratio": 0.3,
            "injection_token_budget": 1200,
            # 任务书 3（注入侧）新参数：两层注入 + 三流 + 去重 + 身份声明
            "injection_enabled": True,
            "stable_layer_enabled": True,
            "identity_declaration_enabled": True,
            "dedup_enabled": True,
            "theme_layer_max": 6,
            "autonomous_flow_threshold": 0.75,
            "fluid_flow_max": 3,
            # 任务书 3B（缓存侧）新参数：cache_control 透传开关
            "cache_control_passthrough": True,
        },
        ensure_ascii=False,
    )
    sid = gen_id("ps")
    conn.execute(
        "INSERT INTO param_snapshots (id, version, params, change_desc, reason, "
        "source, gate_status, is_active, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, "v1.0", default_params, "初始默认参数", "系统初始化种子快照", "manual", "live", 1, now_ms()),
    )
    conn.commit()
    return True


def ensure_event_time_param(conn: sqlite3.Connection) -> None:
    """幂等：活跃快照 JSON 缺 event_time_enabled -> 补默认 true 并 UPDATE。
    不建新快照，不改变其他参数。"""
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        return
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError):
        return
    if "event_time_enabled" not in params:
        params["event_time_enabled"] = True
        conn.execute(
            "UPDATE param_snapshots SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), row["id"]),
        )
        conn.commit()


RETRIEVAL_PARAM_DEFAULTS = {
    "retrieval_cliff_enabled": True,
    "session_entity_fallback": True,
    "shadow_log_enabled": True,
    "event_clue_limit": 200,
    "absolute_floor": 0.3,
    "cliff_gap_min": 0.1,
    "cliff_ratio": 0.3,
    "injection_token_budget": 1200,
}


def ensure_retrieval_params(conn: sqlite3.Connection) -> None:
    """幂等：活跃快照 JSON 缺检索侧新参数 -> 补默认并 UPDATE（仿 ensure_event_time_param，
    不建新快照）。老库（任务书 1 之前的 5 参数快照）自动补齐，重复调用无副作用。"""
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        return
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError):
        return
    changed = False
    for key, value in RETRIEVAL_PARAM_DEFAULTS.items():
        if key not in params:
            params[key] = value
            changed = True
    if changed:
        conn.execute(
            "UPDATE param_snapshots SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), row["id"]),
        )
        conn.commit()


# 任务书 3：注入侧新参数默认值（幂等补参，仿 ensure_event_time_param / ensure_retrieval_params）
INJECTION_PARAM_DEFAULTS = {
    "injection_enabled": True,
    "stable_layer_enabled": True,
    "identity_declaration_enabled": True,
    "dedup_enabled": True,
    "theme_layer_max": 6,
    "autonomous_flow_threshold": 0.75,
    "fluid_flow_max": 3,
    # 任务书 3B（缓存侧）新参数：cache_control 透传开关
    "cache_control_passthrough": True,
    # A18（D1）：旁路审计通道开关（top-N=50 候选全集，供 explain 复盘"那条为什么没进"）
    "audit_enabled": True,
}


def ensure_injection_params(conn: sqlite3.Connection) -> None:
    """幂等：活跃快照 JSON 缺注入侧新参数 -> 补默认并 UPDATE（不建新快照）。
    老库自动补齐，重复调用无副作用。"""
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        return
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError):
        return
    changed = False
    for key, value in INJECTION_PARAM_DEFAULTS.items():
        if key not in params:
            params[key] = value
            changed = True
    if changed:
        conn.execute(
            "UPDATE param_snapshots SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), row["id"]),
        )
        conn.commit()


# B4-7（HC-0815-02 节点5）：学习开关默认值（幂等补参，仿 ensure_injection_params）。
# 开关口令「停止学习/继续学习」写入活跃快照的 learning_enabled；默认 True（继续学习）。
LEARNING_PARAM_DEFAULTS = {
    "learning_enabled": True,
}


def ensure_learning_params(conn: sqlite3.Connection) -> None:
    """幂等：活跃快照 JSON 缺 learning_enabled -> 补默认 True 并 UPDATE（不建新快照）。
    老库自动补齐，重复调用无副作用。"""
    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        return
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError):
        return
    if "learning_enabled" not in params:
        params["learning_enabled"] = True
        conn.execute(
            "UPDATE param_snapshots SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), row["id"]),
        )
        conn.commit()


def ensure_security_schema(conn: sqlite3.Connection) -> dict[str, bool]:
    """P04 幂等迁移：memories / episodes 表补 security_flag 列（老库补列，
    新库建表已含）。照 ensure_b2_schema 先例，可重复调用无副作用。"""
    added = {
        "memories.security_flag": _ensure_column(conn, "memories", "security_flag", "security_flag INTEGER DEFAULT 0"),
        "episodes.security_flag": _ensure_column(conn, "episodes", "security_flag", "security_flag INTEGER DEFAULT 0"),
    }
    conn.commit()
    return added


# [HIPPO] 待确认块持久化表。
# 前身的确认队列只在内存里（`MemorySession.pending_blocks`）——进程一退，挂起的冲突就
# "看不见了"，用户下个会话没法再裁决，而库里那条记忆还挂着 candidate 状态（既不生效
# 也不消失）。"冲突挂起人工确认"是招牌机制，必须跨进程成立。
PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_blocks (
    id          TEXT PRIMARY KEY,
    entries     TEXT NOT NULL,
    conflicts   TEXT DEFAULT '[]',
    reason      TEXT DEFAULT '',
    created_at  INTEGER NOT NULL,
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending_unresolved ON pending_blocks(resolved_at);
"""


def ensure_pending_schema(conn: sqlite3.Connection) -> None:
    """幂等建待确认表（老库补表，重复调用无副作用）。"""
    conn.executescript(PENDING_SCHEMA)
    conn.commit()


def connect(path: Path | str | None = None, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """建/开库（幂等迁移）。

    [HIPPO] path 显式优先 → 兼容锚点 DB_PATH → 当前数据根。check_same_thread 默认 False：
    代理形态与 Agent 形态都会跨线程访问（记忆层用时序锁保护写路径）。
    """
    target = Path(path) if path is not None else (Path(DB_PATH) if DB_PATH else default_db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    ensure_b2_schema(conn)
    seed_initial_snapshot(conn)
    ensure_event_time_param(conn)
    ensure_retrieval_params(conn)
    ensure_injection_params(conn)
    ensure_learning_params(conn)
    ensure_security_schema(conn)
    ensure_pending_schema(conn)
    # [HIPPO] 嵌入档 → 检索参数标定（幂等，只补缺；见 memory/calibration.py）。
    # 放在建库收尾，保证任何入口（CLI／测试／形态层）拿到的库都带正确量纲的阈值。
    try:
        from hippocampus.memory import calibration, config

        calibration.apply_tier_params(conn, config.get_embedding_config()["model"])
    except Exception:
        pass
    return conn


# ---------- 写入 ----------


def add_entity(
    conn: sqlite3.Connection,
    canonical_name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    description: str = "",
) -> str:
    eid = gen_id("e")
    conn.execute(
        "INSERT INTO entities (id, canonical_name, aliases, entity_type, description, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            eid,
            canonical_name,
            json.dumps(aliases or [], ensure_ascii=False),
            entity_type,
            description,
            "active",
            now_ms(),
            now_ms(),
        ),
    )
    return eid


def add_memory(
    conn: sqlite3.Connection,
    mtype: str,
    content: str,
    entity_ids: list[str] | None = None,
    source_quote: str = "",
    source_episode_id: str = "",
    scene_description: str = "",
    scene_tags: list[str] | None = None,
    security_flag: int = 0,
    shadow: int = 0,
    status: str = "active",
) -> str:
    # [HIPPO] 追加 status 参数（默认 "active" = 前身行为不变）：
    # P1 修复与 A34 用 `status='candidate'`（挂起待确认，不参与注入、不参与去重基准）。
    mid = gen_id("m")
    conn.execute(
        "INSERT INTO memories (id, type, status, entity_ids, scene_tags, scene_description, content, "
        "content_lemmatized, source_quote, source_episode_id, change_context, security_flag, shadow, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            mid,
            mtype,
            status,
            json.dumps(entity_ids or [], ensure_ascii=False),
            json.dumps(scene_tags or [], ensure_ascii=False),
            scene_description,
            content,
            "",
            source_quote,
            source_episode_id,
            "",
            int(security_flag or 0),
            int(shadow or 0),
            now_ms(),
            now_ms(),
        ),
    )
    for eid in entity_ids or []:
        add_relation(conn, "memory", mid, "entity", eid, "ABOUT", source_episode_id)
    return mid


def add_episode(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    entity_ids: list[str] | None = None,
    event_time: int | None = None,
    security_flag: int = 0,
) -> str:
    pid = gen_id("ep")
    priority = {"user": "high", "assistant": "medium", "tool": "low"}.get(role, "medium")
    # 语义归位：timestamp=事件时间（缺省=记录时间），created_at=记录时间
    ts = event_time if event_time is not None else now_ms()
    conn.execute(
        "INSERT INTO episodes (id, session_id, timestamp, role, content, priority, entity_ids, derived_memory_ids, security_flag, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            session_id,
            ts,
            role,
            content,
            priority,
            json.dumps(entity_ids or [], ensure_ascii=False),
            "[]",
            int(security_flag or 0),
            now_ms(),
        ),
    )
    for eid in entity_ids or []:
        add_relation(conn, "episode", pid, "entity", eid, "MENTIONS", pid)
    return pid


def add_relation(
    conn: sqlite3.Connection,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    rel_type: str,
    source_episode_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO relations (from_type, from_id, to_type, to_id, rel_type, source_episode_id, extracted_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (from_type, from_id, to_type, to_id, rel_type, source_episode_id, now_ms()),
    )


def supersede_memory(conn: sqlite3.Connection, winner_id: str, loser_id: str, source_episode_id: str = "") -> None:
    """确认机制落库（B1 新增）：胜出记忆保持 active，落败记忆标 superseded，
    并建 SUPERSEDES 关系（胜出方 SUPERSEDES 落败方）。只更新状态，不删任何数据。"""
    conn.execute("UPDATE memories SET status='superseded', updated_at=? WHERE id=?", (now_ms(), loser_id))
    conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (now_ms(), winner_id))
    add_relation(conn, "memory", winner_id, "memory", loser_id, "SUPERSEDES", source_episode_id)


def touch_memory(conn: sqlite3.Connection, memory_id: str, at: int | None = None) -> None:
    """B2（lifecycle 配套）：记录记忆被命中（检索注入/回忆/显式查询）。
    更新 last_hit_at；dormant 记忆被命中视为复活，转回 active（不删任何数据）。"""
    if at is None:
        at = now_ms()
    conn.execute(
        "UPDATE memories SET last_hit_at=?, lifecycle=CASE WHEN lifecycle='dormant' "
        "THEN 'active' ELSE lifecycle END, updated_at=? WHERE id=?",
        (at, at, memory_id),
    )


def set_memory_shadow(conn: sqlite3.Connection, memory_id: str, shadow: int) -> None:
    """轨道B：设置记忆 shadow 标记（0=正式 1=观察期）。"""
    conn.execute("UPDATE memories SET shadow=?, updated_at=? WHERE id=?", (1 if shadow else 0, now_ms(), memory_id))


def promote_shadow(
    conn: sqlite3.Connection, memory_id: str | None = None, where: str = "shadow=1", limit: int = 100
) -> int:
    """轨道B：把 shadow=1 的记忆批量/单条转为正式（shadow=0）。
    memory_id 给定则只转该条；否则转 where 条件的全部（上限 limit）。
    返回转正条数。抽查误提取率低后调用。"""
    if memory_id is not None:
        cursor = conn.execute("SELECT id FROM memories WHERE id=? AND shadow=1", (memory_id,))
    else:
        cursor = conn.execute(f"SELECT id FROM memories WHERE shadow=1 AND {where} LIMIT ?", (limit,))
    ids = [r[0] for r in cursor.fetchall()]
    for mid in ids:
        set_memory_shadow(conn, mid, 0)
    conn.commit()
    return len(ids)


# ---------- 查询 ----------


def get_entity(conn: sqlite3.Connection, eid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    return dict(row) if row else None


def find_entity_by_name(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """消歧第一层：按规范名或别名精确查找"""
    row = conn.execute(
        "SELECT * FROM entities WHERE canonical_name=? OR aliases LIKE ? AND status='active'",
        (name, f'%"{name}"%'),
    ).fetchone()
    return dict(row) if row else None


def get_memories_by_entity(conn: sqlite3.Connection, eid: str, status: str = "active") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.* FROM memories m "
        "JOIN relations r ON r.from_type='memory' AND r.from_id=m.id "
        "WHERE r.to_type='entity' AND r.to_id=? AND r.rel_type='ABOUT' AND m.status=? "
        "ORDER BY m.rowid",
        (eid, status),
    ).fetchall()
    return [dict(r) for r in rows]


def get_episodes_by_entity(conn: sqlite3.Connection, eid: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT e.* FROM episodes e "
        "JOIN relations r ON r.from_type='episode' AND r.from_id=e.id "
        "WHERE r.to_type='entity' AND r.to_id=? AND r.rel_type='MENTIONS' "
        "ORDER BY e.timestamp DESC",
        (eid,),
    ).fetchall()
    return [dict(r) for r in rows]


def ensure_import_schema(conn: sqlite3.Connection) -> None:
    """任务书 4：导入确认池 + 导入运行记录两表（幂等，仿 ensure_retrieval_params 风格）。
    可重复调用不报错。"""
    conn.executescript("""
CREATE TABLE IF NOT EXISTS import_pending (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    mtype             TEXT NOT NULL,
    content           TEXT NOT NULL,
    entity_ids        TEXT DEFAULT '[]',
    source_session_id TEXT DEFAULT '',
    source_title      TEXT DEFAULT '',
    source_quote      TEXT DEFAULT '',
    event_time        INTEGER,
    memory_id         TEXT DEFAULT '',
    conflict_with     TEXT DEFAULT '',
    status            TEXT DEFAULT 'pending',
    created_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    id                  TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    state               TEXT DEFAULT 'scanning',
    total_sessions      INTEGER DEFAULT 0,
    processed_sessions  INTEGER DEFAULT 0,
    processed_ids       TEXT DEFAULT '[]',
    current_title       TEXT DEFAULT '',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ip_account ON import_pending(account_id, status);
CREATE INDEX IF NOT EXISTS idx_ir_account ON import_runs(account_id);
""")
    conn.commit()


def count_stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "memories": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        "episodes": conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
        "relations": conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
    }
