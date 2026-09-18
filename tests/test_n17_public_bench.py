"""N17 公开基准适配层（LoCoMo／LongMemEval）：加载、导入、打分口径各钉一条。

边界（与结果表一致）：这里的分数是**离线检索/词面口径**（无模型端点 → 不用大模型作答、
不做 LLM 判分）；本文件只验证"加载/导入/判据/离线闸"四件事，不产出分数。
"""

from __future__ import annotations

import json

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.eval import public_bench as pb


def test_normalize_variants_and_f1():
    assert pb.normalize_text("Hello, World!") == "hello world"
    # 日期类答案要出 ISO 变体（LoCoMo 时间题的答案来自会话时间戳）
    assert "2023-05-07" in pb.answer_variants("7 May 2023")
    assert "2023-05" in pb.answer_variants("May 2023")
    assert pb.answer_variants("Business Administration") == {"business administration"}
    assert pb.token_f1("business administration", "Business Administration") == pytest.approx(1.0)
    assert pb.token_f1("", "x") == 0.0


def test_load_locomo_synthetic(tmp_path):
    """合成一份 LoCoMo 形状的数据：加载后题目/证据/对话轮都要对得上。"""
    data = [
        {
            "sample_id": "conv-x",
            "conversation": {
                "speaker_a": "A",
                "speaker_b": "B",
                "session_1_date_time": "1:56 pm on 8 May, 2023",
                "session_1": [
                    {"speaker": "A", "dia_id": "D1:1", "text": "I adopted a cat named Momo."},
                    {"speaker": "B", "dia_id": "D1:2", "text": "That is lovely."},
                ],
            },
            "qa": [
                {"question": "What is my cat called?", "answer": "Momo", "evidence": ["D1:1"], "category": 4},
                {"question": "When did I adopt a cat?", "answer": "8 May 2023", "evidence": ["D1:1"], "category": 2},
            ],
        }
    ]
    path = tmp_path / "locomo_tiny.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    items = pb.load_locomo(path)
    assert len(items) == 2
    assert items[0].group == items[1].group == "conv-x"
    assert items[0].category == "cat4"
    assert items[0].evidence_texts == ["I adopted a cat named Momo."]
    assert items[0].turns[0].speaker == "A"
    assert items[0].turns[0].ts_ms is not None, "会话时间要解析成毫秒时间戳"


def test_load_longmemeval_synthetic(tmp_path):
    data = [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "What degree did I graduate with?",
            "answer": "Business Administration",
            "haystack_dates": ["2023/05/30 (Tue) 23:40"],
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "I graduated with a degree in Business Administration."}]],
            "answer_session_ids": ["s1"],
        }
    ]
    path = tmp_path / "lme_tiny.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    items = pb.load_longmemeval(path)
    assert len(items) == 1
    assert items[0].evidence_texts == ["I graduated with a degree in Business Administration."]
    assert items[0].category == "single-session-user"


def test_ingest_history_counts_and_pools(core, scope):
    """批量导入：经历层与记忆条各落一份；实体只挂说话人（机械归属）。"""
    out = core.ingest_history(
        scope,
        [
            {"text": "我住在天津", "role": "user", "speaker": "A"},
            {"text": "天津不错", "role": "assistant", "speaker": "B"},
        ],
    )
    assert out == {"episodes": 2, "memories": 2, "skipped": 0}
    stats = core.stats(scope)
    assert stats["memories"] == 2 and stats["episodes"] == 2
    assert stats["entities"] >= 2, "说话人应建成实体"


def test_date_prefix_makes_temporal_answer_visible(core):
    """协议钉住：会话日期前缀 + 答案 ISO 变体 → 时间题才可能判"答在文内"。

    LoCoMo 的时间题答案（"8 May 2023"）在原文里**不出现**，只存在于会话时间戳里；
    不写日期前缀就等于对全部时间题系统性误判。
    """
    item = pb.Item(
        qid="q-date",
        question="When did I adopt a cat?",
        answers=["8 May 2023"],
        category="cat2",
        evidence_texts=["I adopted a cat named Momo."],
        turns=[pb.Turn(text="I adopted a cat named Momo.", ts_ms=1683558960000, session_id="s1", speaker="A")],
        group="conv-date",
    )
    report = pb.run_items(core, [item], account_prefix="t-date")
    assert report["overall"]["n"] == 1
    assert report["rows"][0]["answer_in_context"] is True, "日期前缀应让时间题可判命中"
    assert report["rows"][0]["evidence_in_context"] is True


def test_mismatching_date_is_not_counted_as_hit(core):
    """参考答案的日期与语料里的日期不一致 → **不算命中**（防日期判据假阳性）。"""
    item = pb.Item(
        qid="q-date2",
        question="When did I adopt a cat?",
        answers=["8 May 2023"],
        category="cat2",
        evidence_texts=["I adopted a cat named Momo."],
        turns=[pb.Turn(text="I adopted a cat named Momo.", ts_ms=1614692400000, session_id="s1", speaker="A")],
        group="conv-date2",
    )
    report = pb.run_items(core, [item], account_prefix="t-date2")
    assert report["rows"][0]["answer_in_context"] is False


def test_episode_rendering_carries_session_date(core, scope):
    """记忆层**自己**会把经历的时间渲染进注入（`[user 2023-05-08] “…”`）。

    这是"时间题可判"的机制来源；`date_prefix` 只是把同一信息也带进记忆条（经验层命中时也有日期）。
    """
    core.ingest_history(
        scope,
        [{"text": "I adopted a cat named Momo.", "role": "user", "ts_ms": 1683558960000, "session_id": "s1"}],
        as_memories=False,  # 只进经历层，单看事件通道的渲染
    )
    text = core.inject_finalize(scope, "When did I adopt a cat?").text
    assert "2023-05-08" in text, "事件层注入应带会话日期"


def test_bench_requires_offline(tmp_path, monkeypatch):
    """硬闸：非离线档跑基准直接抛（防止"有 key 环境里跑基准 → 维护链去打模型端点"）。"""
    monkeypatch.delenv("HIPPOCAMPUS_OFFLINE", raising=False)
    monkeypatch.setattr("hippocampus.settings.is_offline", lambda: False)
    with pytest.raises(RuntimeError, match="离线档"):
        pb.run(bench="locomo", data=tmp_path / "nope.json", home=tmp_path / "home")


def test_render_has_boundary_note(core):
    """结果表必须自带口径声明（检索/词面口径、官方分需显式开关），不让读者自己猜。"""
    item = pb.Item(
        qid="q1",
        question="Where do I live?",
        answers=["天津"],
        category="cat4",
        evidence_texts=["我住在天津"],
        turns=[pb.Turn(text="我住在天津", session_id="s1", speaker="A")],
        group="conv-r",
    )
    report = pb.run_items(core, [item], account_prefix="t-render")
    text = pb.render(report)
    assert "检索/词面口径" in text and "官方分" in text
    assert "总体" in text


def test_sha256_file(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{}", encoding="utf-8")
    assert len(pb.sha256_file(p)) == 64
    assert pb.sha256_file(tmp_path / "missing.json") == ""


def test_memory_core_ingest_is_idempotent(core, scope):
    """导入精确去重（C1 六轮）：同内容重复导入只入一次，不覆盖既有记忆。

    磨平 T10 教训（复用 home 重跑把库滚大、污染内存与命中数字）——同批历史
    二跑，记忆/经历两池都判重跳过，行数不变；返回 skipped 如实计数。
    """
    turns = [{"text": "重复的一句话", "role": "user"}]
    out1 = core.ingest_history(scope, turns)
    stats = core.stats(scope)
    before = stats["memories"]
    out2 = core.ingest_history(scope, turns)
    assert core.stats(scope)["memories"] == before
    assert out1["skipped"] == 0 and out2["skipped"] == 1
    assert out2["memories"] == 0 and out2["episodes"] == 0


def test_scope_isolation_between_bench_accounts(home):
    """基准按 account 隔离：A 账号导入的东西不会出现在 B 账号的检索里。"""
    core = MemoryCore(home=home)
    try:
        a = Scope(account="bench-a", session="bench", source="user")
        b = Scope(account="bench-b", session="bench", source="user")
        core.ingest_history(a, [{"text": "我的期望城市是杭州", "role": "user"}])
        hit_a = core.inject_finalize(a, "我的期望城市是哪里")
        hit_b = core.inject_finalize(b, "我的期望城市是哪里")
        assert any("杭州" in i.content for i in hit_a.items)
        assert not hit_b.items, "另一个 account 不该检索到 A 的库"
    finally:
        core.close()
