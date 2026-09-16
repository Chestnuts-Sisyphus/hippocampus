# 由前身 hippocampus_prototype/dedup.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""Hippocampus P1 —— 记忆去重（dedup.py，HC-0815-02 节点1 新增）

发现：preference 语义重复 45%（22 条中 5 组重复：3 组完全 + 2 组语义），
完全重复 48 条（24 组）。两级去重：

  ① 完全重复：content 规范化（全角转半角 + 压缩空白）后与库内同类型 active
     正式记忆完全相等（纯 SQL，零依赖，导入路径可用）。
  ② 语义重复：embedding 余弦相似度 ≥ 阈值。MiniLM 中文 embedding 虚高是
     已知问题（发现 22/29 同根）→ 阈值分层保守取 0.85（英文常用 0.7 会误杀
     中文短文本）；换 embedding 模型前必须先小样本验证中文检索质量（本模块
     不换型，只做阈值防御）。

只与 status='active' AND shadow=0 的正式记忆比较（轨道B 观察期记忆不参与
去重基准，避免观察期噪声压制正式记忆）；去重任何异常一律软失败返回 None
（绝不阻断入库主流程）。
"""

import re
import sqlite3
import unicodedata

# 语义重复阈值：中文 MiniLM 虚高 → 比英文常用 0.7 高，防误杀（发现 22 实锤）
SEMANTIC_DUP_THRESHOLD = 0.85

# 语义近邻查询条数（够了——同主题重复一般排在最前）
_SEMANTIC_TOP_N = 5

# 完全重复比较时剔除的标点（中英文句读类；「深色主题」vs「深色主题。」= 重复）
_PUNCT_RE = re.compile(r"[，。！？；：、‘’“”（）《》【】,\.!?;:'\"()\[\]<>]")

# HC-0901-01：语义命中后的值变更分类信号
# 否定字（去后相等=极性翻转，如「喜欢深色」vs「不喜欢深色」）
_NEG_RE = re.compile(r"[不没别无非莫勿]")
# 数值（阿拉伯+中文数字；集合不同=值变更，如「42 码」→「40 码」）
_NUM_RE = re.compile(r"\d+|[零一二两三四五六七八九十百千万]+")
# 连续拉丁/数字段（删除后相等=同构换值，如「我用 VS Code 写代码」→「我用 Vim 写代码」）
_LATIN_NUM_RE = re.compile(r"[A-Za-z0-9]+")


def normalize_content(content: str) -> str:
    """完全重复比较用规范化：全角转半角 + 剔除标点 + 压缩空白 + 去首尾空白。"""
    if not content:
        return ""
    text = unicodedata.normalize("NFKC", content)
    text = _PUNCT_RE.sub("", text)
    return " ".join(text.split())


def classify_dup_action(old_content: str, new_content: str) -> str:
    """语义去重命中后的三态分类（HC-0901-01；纯机械零 LLM 零 embedding）。

    背景：同构变更句（「深色→浅色」「VS Code→Vim」「42码→40码」）embedding 高分
    被语义去重命中后若一律丢弃 = 用户改主意永远不生效（0816-02 定标 §2b 实锤，
    MiniLM 现网 8/14 变更句被吞）。本函数对命中对做机械分流，纯增益设计：
      - "supersede"：机械确认同对象值变更（新值取代旧值，走 SUPERSEDES）
        ①数值集合不同（42→40）②去否定字后规范化相等（喜欢↔不喜欢深色）
      - "admit"：同构换值——删连续拉丁/数字段后相等（英文/数字枚举差异，
        VS Code↔Vim），放行入库（宁冗余不丢变更）
      - "drop"：其余（改写型重复保守拦截面，维持去重现状；调用方打观察日志，
        真实误拦样本回流下一刀精化判据）
    任何异常返回 "drop"（=现状行为，绝不阻断入库主流程）。"""
    try:
        if not old_content or not new_content:
            return "drop"
        norm_old = normalize_content(old_content)
        norm_new = normalize_content(new_content)
        if not norm_old or not norm_new or norm_old == norm_new:
            return "drop"
        # ①数值集合不同 → 值变更
        old_nums, new_nums = set(_NUM_RE.findall(norm_old)), set(_NUM_RE.findall(norm_new))
        if old_nums and new_nums and old_nums != new_nums:
            return "supersede"
        # ②去否定字后相等 → 极性翻转变更
        if _NEG_RE.sub("", norm_old) == _NEG_RE.sub("", norm_new):
            return "supersede"
        # ③删拉丁/数字段后相等 → 同构换值，放行（删段后重压缩空白：VS Code 与 Vim
        # 删后空格数不同，须归一再比）
        stripped_old = " ".join(_LATIN_NUM_RE.sub("", norm_old).split())
        stripped_new = " ".join(_LATIN_NUM_RE.sub("", norm_new).split())
        if stripped_old and stripped_new and stripped_old == stripped_new:
            return "admit"
        return "drop"
    except Exception:
        return "drop"


def find_exact_duplicate(
    conn: sqlite3.Connection, mtype: str, content: str, exclude_session_id: str | None = None
) -> str | None:
    """完全重复：同类型 active 正式记忆 content 规范化相等 → 返回已存在 id，否则 None。

    exclude_session_id：排除同一会话内已入库的记忆（tracka 验收语义：同会话
    跨轮重复允许再入库；P1 观察数据的重复主要来自跨会话导入）。"""
    norm = normalize_content(content)
    if not norm:
        return None
    if exclude_session_id:
        rows = conn.execute(
            "SELECT m.id, m.content FROM memories m "
            "LEFT JOIN episodes e ON m.source_episode_id = e.id "
            "WHERE m.type=? AND m.status='active' AND COALESCE(m.shadow,0)=0 "
            "AND (e.id IS NULL OR COALESCE(e.session_id,'') != ?)",
            (mtype, exclude_session_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE type=? AND status='active' AND COALESCE(shadow,0)=0",
            (mtype,),
        ).fetchall()
    for row in rows:
        if normalize_content(row["content"] or "") == norm:
            return row["id"]
    return None


def semantic_dup_threshold(conn: sqlite3.Connection | None = None) -> float:
    """语义重复阈值。活跃参数快照的 `semantic_dup_threshold` 优先（按嵌入档标定），
    否则用模块常量（前身默认 0.85）。[HIPPO] 分档标定见 memory/calibration.py。"""
    if conn is not None:
        try:
            import json as _json

            row = conn.execute("SELECT params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
            if row:
                value = _json.loads(row["params"]).get("semantic_dup_threshold")
                if isinstance(value, (int, float)) and 0 < value <= 1:
                    return float(value)
        except Exception:
            pass
    return SEMANTIC_DUP_THRESHOLD


def find_semantic_duplicate(
    conn: sqlite3.Connection,
    collections: dict | None,
    mtype: str,
    content: str,
    exclude_session_id: str | None = None,
) -> str | None:
    """语义重复：embedding 近邻中同类型 active 正式记忆 sim ≥ 阈值 → 返回已存在 id，否则 None。

    collections 为 None（调用方无 chroma 句柄，如导入路径）→ None；
    chroma/embedding 任何异常 → None（软失败，只做完全去重）。
    exclude_session_id：同会话已有记忆不参与基准（与 find_exact_duplicate 同语义）。"""
    if collections is None or not content or not content.strip():
        return None
    threshold = semantic_dup_threshold(conn)
    try:
        from hippocampus.memory import retrieval as rt

        q_emb = rt._query_embedding(content)
        hits = rt.semantic_search(collections["mem"], content, n=_SEMANTIC_TOP_N, query_embedding=q_emb)
    except Exception:
        return None
    best_id: str | None = None
    best_sim = 0.0
    for doc_id, v in hits.items():
        if v.get("kind") != "memory" or v.get("sim", 0.0) < threshold:
            continue
        if exclude_session_id:
            row = conn.execute(
                "SELECT m.id FROM memories m "
                "LEFT JOIN episodes e ON m.source_episode_id = e.id "
                "WHERE m.id=? AND m.type=? AND m.status='active' AND COALESCE(m.shadow,0)=0 "
                "AND (e.id IS NULL OR COALESCE(e.session_id,'') != ?)",
                (doc_id, mtype, exclude_session_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM memories WHERE id=? AND type=? AND status='active' AND COALESCE(shadow,0)=0",
                (doc_id, mtype),
            ).fetchone()
        if row and v["sim"] > best_sim:
            best_id = doc_id
            best_sim = v["sim"]
    return best_id


def find_duplicate(
    conn: sqlite3.Connection,
    collections: dict | None,
    mtype: str,
    content: str,
    exclude_session_id: str | None = None,
) -> str | None:
    """两级去重入口：先完全后语义（collections 为 None 时只做完全去重）。

    语义去重只对 preference 生效（6c 收口）：观察线 P1 的语义重复数据集中在
    preference（22 条中 5 组）；fact/resource/status 同主题不同值可能是「更新/
    矛盾」（如「用 PostgreSQL」→「用 SQLite」），应放行进冲突检测走 A5-2 更新
    窗口/SUPERSEDES，不能当重复拦截（否则更新窗口死锁）。fact 完全重复仍拦。"""
    exact = find_exact_duplicate(conn, mtype, content, exclude_session_id=exclude_session_id)
    if exact is not None:
        return exact
    if mtype != "preference":
        return None
    return find_semantic_duplicate(conn, collections, mtype, content, exclude_session_id=exclude_session_id)
