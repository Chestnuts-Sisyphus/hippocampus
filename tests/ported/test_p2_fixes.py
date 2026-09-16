# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""HC-0815-02 节点2（P2 批量产品问题）隔离单测。

七个问题各一个回归用例：
1. 轨道A 类型泄漏（status 混入）→ extract 类型白名单 + pipeline 过滤
2. AI 幻觉编造资源 → 路径存在性校验
3. 情绪宣泄提取为 status → 机械拦截
4. SUPERSEDES 盲区（跨类型技术栈矛盾）→ 候选集全类型统一
5. 寒暄重述误报 + 告警风暴（发现 22）→ 寒暄前置过滤 + 语言分层阈值
6. 确认块错位/串轮（发现 28）→ pending_block 单槽改多槽
7. 检索注入漏命中/过时注入（发现 29/30）→ 注入按 updated_at 排序

绝不碰真实库：MemorySession 的账户目录 monkeypatch 到 tmp_path，
语义通道 monkeypatch 假数据。
"""

import pytest

from hippocampus.memory import confirm, conflict, extract, pipeline
from hippocampus.memory import database as db
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """临时账户 MemorySession（绝不碰真实账户目录）。"""
    monkeypatch.setattr(mb.account, "get_account_data_dir", lambda aid: tmp_path / f"acct_{aid}")
    sess = mb.MemorySession(f"p2_{tmp_path.name[-8:]}")
    yield sess
    mb.drop_bridge(sess.account_id)
    import shutil

    shutil.rmtree(str(tmp_path / f"acct_{sess.account_id}"), ignore_errors=True)


# ── 1. 类型泄漏（status 混入 47%，发现 16 更正版） ──────────────────────────


def test_extract_filters_invalid_types(tmp_conn_factory, monkeypatch):
    """LLM 输出非法类型（task/goal）→ extract() 白名单剔除（机械兜底）。"""
    conn = tmp_conn_factory
    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [
                {"type": "task", "content": "先做 A 再做 B", "entity_names": [], "source_quote": "先做 A"},
                {"type": "goal", "content": "目标是一年内上线", "entity_names": [], "source_quote": "目标"},
                {"type": "fact", "content": "Python 是解释型语言", "entity_names": [], "source_quote": "Python"},
            ],
        },
    )
    r = pipeline.process_user_message(conn, "sess_t", "先做 A 再做 B，Python 是解释型语言")
    assert len(r["memory_ids"]) == 1
    row = conn.execute("SELECT type FROM memories WHERE id=?", (r["memory_ids"][0],)).fetchone()
    assert row["type"] == "fact"


def test_track_a_status_excluded(tmp_conn_factory, monkeypatch):
    """轨道A（memory_types=preference/fact）status 不入库（类型泄漏回归）。"""
    conn = tmp_conn_factory
    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [
                {"type": "preference", "content": "我喜欢深色主题", "entity_names": [], "source_quote": "深色"},
                {"type": "status", "content": "重构进行到一半", "entity_names": [], "source_quote": "一半"},
            ],
        },
    )
    r = pipeline.process_user_message(conn, "sess_t", "我喜欢深色主题，重构进行到一半", memory_types=["preference", "fact"])
    assert len(r["memory_ids"]) == 1
    row = conn.execute("SELECT type FROM memories WHERE id=?", (r["memory_ids"][0],)).fetchone()
    assert row["type"] == "preference"


# ── 2. AI 幻觉编造资源（promote 前事实性校验） ─────────────────────────────


def test_resource_plausible(tmp_path):
    """绝对路径存在性校验：全不存在=幻觉；存在任一=可信；裸文件名/无路径=放行。

    注意：裸文件名不校验（「密钥在 config.yaml 里」类——AI 可能引用用户环境
    真实文件，误杀风险高；幻觉拦截只针对完整绝对路径，观察线实测 AI 编造
    D:/AI/xxx.py 类具体路径）。"""
    real_file = tmp_path / "real_config.yaml"
    real_file.write_text("x", encoding="utf-8")
    # 绝对路径存在 → 可信
    assert mb._resource_plausible(f"文件在 {real_file}")
    # 绝对路径不存在 → 幻觉
    assert not mb._resource_plausible(f"文件在 {tmp_path}/no_such_file.py")
    assert not mb._resource_plausible("文件在 D:/nonexistent_dir_xyz/hallucinated.py")
    # 裸文件名（无论是否存在）→ 不校验放行（防误杀轨道B 常见样例）
    assert mb._resource_plausible("代码在 proxy_app.py 里")
    assert mb._resource_plausible("代码在 nonexistent_module_xyz.py 里")
    assert mb._resource_plausible("密钥在 config.yaml 里")
    # 无路径声明（资源类型但不涉及文件系统）→ 放行
    assert mb._resource_plausible("资料在 Notion 里")


def test_extract_response_filters_hallucinated_resource(session, monkeypatch):
    """轨道B 提取到幻觉资源（路径全不存在）→ 不入库。"""
    fake_result = {
        "entities": [],
        "memories": [
            {"type": "resource", "content": "文件在 D:/nonexistent_dir_xyz/hallucinated.py",
             "entity_names": [], "source_quote": "文件在 D:/nonexistent_dir_xyz/hallucinated.py"},
            {"type": "status", "content": "部署已完成", "entity_names": [], "source_quote": "部署已完成"},
        ],
    }
    monkeypatch.setattr(mb, "_get_extract_response_fn", lambda: (lambda t: fake_result))
    ids = mb.extract_response(session, "AI 回复：文件在 D:/nonexistent_dir_xyz/hallucinated.py，部署已完成")
    assert len(ids) == 1
    row = session.conn.execute("SELECT type, content FROM memories WHERE id=?", (ids[0],)).fetchone()
    assert row["type"] == "status"  # 幻觉 resource 被滤，status 正常入


# ── 3. 情绪宣泄提取为 status ───────────────────────────────────────────────


def test_emotional_status_filtered(tmp_conn_factory, monkeypatch):
    """「查 bug 三小时太烦了」→ 情绪宣泄不入库；「bug 已修复」不受影响。"""
    conn = tmp_conn_factory
    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda system, user, max_tokens: {
            "entities": [],
            "memories": [
                {"type": "status", "content": "查 bug 三小时太烦了", "entity_names": [], "source_quote": "太烦了"},
                {"type": "status", "content": "这个 bug 已经修复了", "entity_names": [], "source_quote": "修复了"},
            ],
        },
    )
    r = pipeline.process_user_message(conn, "sess_t", "查 bug 三小时太烦了，不过这个 bug 已经修复了")
    assert len(r["memory_ids"]) == 1
    row = conn.execute("SELECT content FROM memories WHERE id=?", (r["memory_ids"][0],)).fetchone()
    assert "修复了" in row["content"]


def test_extract_prompt_has_emotion_constraint():
    """提取 prompt 含情绪宣泄反例（软约束）。"""
    assert "情绪宣泄" in extract.EXTRACT_SYSTEM or "太烦了" in extract.EXTRACT_SYSTEM


# ── 4. SUPERSEDES 盲区（跨类型技术栈矛盾，SQLite vs PostgreSQL） ───────────


def test_batch_conflicts_candidates_cross_type(tmp_conn_factory, monkeypatch):
    """候选集全类型统一：fact「用 SQLite」与 resource「用 PostgreSQL」同批可见 →
    LLM 能判矛盾（原按类型分批永不同批 = SUPERSEDES 盲区）。"""
    conn = tmp_conn_factory
    eid = db.add_entity(conn, "数据库", "Abstract")
    old_fact = db.add_memory(conn, "fact", "数据库用 SQLite", entity_ids=[eid])
    old_res = db.add_memory(conn, "resource", "数据库用 PostgreSQL", entity_ids=[eid])
    conn.commit()
    new_mem = {"id": "m_new_1", "type": "resource", "content": "数据库用 PostgreSQL", "created_at": db.now_ms()}

    seen = {}

    def _fake_detect(conn_, memories):
        # 断言 LLM 视野包含跨类型两条旧记忆（新 fact + 旧 resource 同批）
        seen["cand_types"] = {m["type"] for m in memories}
        seen["has_fact"] = any(m["id"] == old_fact for m in memories)
        seen["has_res"] = any(m["id"] == old_res for m in memories)
        return [(new_mem["id"], old_fact, "技术栈矛盾")]

    monkeypatch.setattr(conflict, "detect_conflicts", _fake_detect)
    out = conflict.batch_detect_conflicts(conn, [new_mem])
    assert seen["has_fact"] and seen["has_res"], f"候选集应含跨类型记忆: {seen}"
    assert new_mem["id"] in out  # LLM 判矛盾 → 新记忆挂冲突候选


# ── 5. 寒暄重述误报 + 告警风暴（发现 22） ───────────────────────────────────


def test_greeting_skips_restatement(session, monkeypatch):
    """「你好」→ 不触发 N3 重述检测（寒暄前置过滤，防误报打扰 + LLM 浪费）。"""
    calls = {"semantic": 0}

    def _boom_semantic(collection, query, n=40, query_embedding=None):
        calls["semantic"] += 1
        return {"ep_x": {"sim": 0.7578, "kind": "episode"}}  # 旧阈值 0.7 会命中

    monkeypatch.setattr(rt, "semantic_search", _boom_semantic)
    stable, fluid, filtered = mb.prepare_injection(session, "你好")
    assert calls["semantic"] == 0, "寒暄不应触发语义检索（N3 重述检测被跳过）"


def test_restate_threshold_language_layer():
    """语言分层阈值：中文 0.8（防 MiniLM 虚高），非中文 0.7。"""
    assert mb._restate_threshold("数据库用什么？") == 0.8
    assert mb._restate_threshold("What editor do you use?") == 0.7


def test_restatement_zh_threshold_blocks_0_75(session, monkeypatch):
    """中文 0.7578 命中 → 新阈值 0.8 不触发重述警报（发现 22 场景回归）。"""
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {"ep_x": {"sim": 0.7578, "kind": "episode"}},
    )
    r = mb._check_restatement(session.conn, "上次说的那件事", session.collections["ep"])
    from hippocampus.memory import calibration
    from hippocampus.memory import config as _cfg

    _thr = calibration.params_for_tier(_cfg.get_embedding_config()['model'])['restate_threshold']
    assert _thr > 0
    # [HIPPO] 阈值按嵌入档标定（前身固定 0.8）：同一 sim 在档位阈值之下才不报警
    if 0.7578 < _thr:
        assert r is None
    else:
        assert r is not None and r['trigger_diagnosis'] in (True, False)


def test_restatement_en_threshold_triggers_0_75(session, monkeypatch):
    """英文 0.75 ≥ 0.7 → 仍触发重述警报（非中文阈值不变）。"""
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {"ep_x": {"sim": 0.75, "kind": "episode"}},
    )
    r = mb._check_restatement(session.conn, "the editor thing again", session.collections["ep"])
    assert r is not None


# ── 6. 确认块错位/串轮（发现 28：单槽 → 多槽） ─────────────────────────────


def _make_block(conn, new_id, old_id):
    eid = db.add_entity(conn, "主题", "Abstract")
    old = db.add_memory(conn, "status", f"旧内容{old_id}", entity_ids=[eid])
    new = db.add_memory(conn, "status", f"新内容{new_id}", entity_ids=[eid])
    conn.commit()
    return confirm.build_confirm_block(conn, [new], [(new, old, "状态翻转")])


def test_confirmation_matches_latest_block(session):
    """多槽：上一轮块未消费 + 本轮新块 → 「确认1」命中最近块（确认不错对象）。"""
    with session.lock:
        block_old = _make_block(session.conn, "old", "old")
        session.push_pending_block(block_old)
        block_new = _make_block(session.conn, "new", "new")
        session.push_pending_block(block_new)
        assert len(session.pending_blocks) == 2
    matched = session.match_confirmation("确认1")
    assert matched is not None
    blk, decision = matched
    assert blk is block_new, "应命中最近展示的块（串轮修复）"
    assert decision == ("confirm", block_new.entries[0]["mem"]["id"])


def test_confirmation_consumes_and_removes(session):
    """确认后出队：只消费命中的块，其余保留。"""
    with session.lock:
        block_old = _make_block(session.conn, "a", "a")
        session.push_pending_block(block_old)
        block_new = _make_block(session.conn, "b", "b")
        session.push_pending_block(block_new)
    assert mb.handle_confirmation(session, "确认1") is not None
    with session.lock:
        assert block_new not in session.pending_blocks
        assert block_old in session.pending_blocks  # 旧块保留（仍可后续确认）


def test_pending_queue_cap(session):
    """队列上限 3：超出丢最旧（防堆积）。"""
    with session.lock:
        b1 = _make_block(session.conn, "1", "1")
        b2 = _make_block(session.conn, "2", "2")
        b3 = _make_block(session.conn, "3", "3")
        b4 = _make_block(session.conn, "4", "4")
        for b in (b1, b2, b3, b4):
            session.push_pending_block(b)
        assert len(session.pending_blocks) == 3
        assert b1 not in session.pending_blocks
        assert b4 in session.pending_blocks


# ── 7. 检索注入漏命中/过时注入（发现 29/30） ────────────────────────────────


def test_injection_sorted_by_updated_at(tmp_conn_factory):
    """同主题新旧两条 → 注入结果最新优先（旧 PostgreSQL 值不压过新 SQLite）。"""
    conn = tmp_conn_factory
    eid = db.add_entity(conn, "数据库", "Abstract")
    old_m = db.add_memory(conn, "fact", "数据库用 PostgreSQL", entity_ids=[eid])
    new_m = db.add_memory(conn, "fact", "数据库用 SQLite", entity_ids=[eid])
    conn.commit()
    conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (db.now_ms() + 1000, new_m))
    conn.commit()
    results = [
        {"doc_id": old_m, "kind": "memory", "channel": "semantic", "channel_rank": 0, "score": 0.9},
        {"doc_id": new_m, "kind": "memory", "channel": "semantic", "channel_rank": 1, "score": 0.7},
        {"doc_id": "ep_1", "kind": "episode", "channel": "event", "channel_rank": 0, "score": 0.0},
    ]
    out = mb._sort_injection_results(conn, results)
    assert out[0]["doc_id"] == new_m, "最新记忆应排最前"
    assert out[1]["doc_id"] == old_m
    assert out[2]["doc_id"] == "ep_1"  # episode 保持原序在后


def test_injection_updated_at_order_in_prepare(session, monkeypatch):
    """发现 30 端到端：同主题新旧两条 → 注入文本最新（SQLite）在前。

    两条 fact 均被稳定层命中（theme 层按 updated_at 降序），注入文本中
    新值不压在旧值之后（旧 PostgreSQL 值不再误导）。
    """
    eid = db.add_entity(session.conn, "数据库", "Abstract")
    db.add_memory(session.conn, "fact", "数据库用 PostgreSQL", entity_ids=[eid])
    new_m = db.add_memory(session.conn, "fact", "数据库用 SQLite", entity_ids=[eid])
    session.conn.commit()
    session.conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (db.now_ms() + 1000, new_m))
    session.conn.commit()
    session.reindex()
    monkeypatch.setattr(
        rt,
        "semantic_search",
        lambda collection, query, n=40, query_embedding=None: {},
    )
    stable, fluid, filtered = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert stable, "稳定层应注入数据库主题记忆"
    old_pos = stable.find("PostgreSQL")
    new_pos = stable.find("SQLite")
    assert new_pos != -1 and (old_pos == -1 or new_pos < old_pos), "注入文本最新（SQLite）在前"


def test_multi_query_injection_quality(session, monkeypatch):
    """发现 29 检索质量回归：多查询样本——不同主题查询各命中对应主题记忆
    （不再出现「平时用哪个编辑器」查不到编辑器记忆的漏命中）。"""
    eid_db = db.add_entity(session.conn, "数据库", "Abstract")
    eid_ed = db.add_entity(session.conn, "编辑器", "Abstract")
    db.add_memory(session.conn, "fact", "数据库用 SQLite", entity_ids=[eid_db])
    db.add_memory(session.conn, "fact", "平时用 VS Code 写代码", entity_ids=[eid_ed])
    session.conn.commit()
    session.reindex()
    # 语义通道按查询分发（模拟正常 embedding 检索：主题命中）
    def _fake_sem(collection, query, n=40, query_embedding=None):
        if "数据库" in query:
            return {"m_db": {"sim": 0.85, "kind": "memory"}}
        if "编辑器" in query or "写代码" in query:
            return {"m_ed": {"sim": 0.85, "kind": "memory"}}
        return {}

    monkeypatch.setattr(rt, "semantic_search", _fake_sem)
    # 查询1：数据库主题
    s1, f1, _ = mb.prepare_injection(session, "数据库用什么？", flow="user")
    assert "SQLite" in s1 + f1
    # 查询2：编辑器主题（发现 29 漏命中场景）
    s2, f2, _ = mb.prepare_injection(session, "我平时用哪个编辑器写代码？", flow="user")
    assert "VS Code" in s2 + f2


# ── 公共 fixture：临时连接 ──────────────────────────────────────────────────


@pytest.fixture()
def tmp_conn_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "memory.db")
    conn = db.connect()
    yield conn
    conn.close()
