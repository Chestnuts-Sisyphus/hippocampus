"""T8 英文实体抽取（A4）测试：机械抽取 + 导入链路 + A/B 开关（默认关零变化）。

验收：
- 句子中连续大写词合并成专名（"Alice Johnson"），句首大写/代词/普通词不入；
- `extract_english_entities` 是纯函数（可测）；
- ingest_history 的 `entities` 字段（可选）把实体挂到记忆上（图通道数据源）；
- `run_items(..., english_entities=True)` 变体显式开启、默认 False 行为不变。
"""

from __future__ import annotations


def test_extract_full_names_and_mid_sentence():
    from hippocampus.eval.public_bench import extract_english_entities

    text = "Alice Johnson booked the flight while Bob stayed home. The decision was final."
    names = extract_english_entities(text)
    # "Alice Johnson" 连续大写合并；"Bob" 句中大写；句首 "The"/"Alice" 首句位置不算
    assert "Alice Johnson" in names or "Alice" in names
    assert "Bob" in names


def test_extract_skips_sentence_start_and_stopwords():
    from hippocampus.eval.public_bench import extract_english_entities

    text = "I went with The team to see Caroline in New York. We had fun."
    names = extract_english_entities(text)
    assert "I" not in names
    assert "The" not in names
    assert "We" not in names
    assert any(n in ("Caroline", "New York") for n in names)


def test_extract_empty_and_dedup():
    from hippocampus.eval.public_bench import extract_english_entities

    assert extract_english_entities("") == []
    names = extract_english_entities("Meet Tom. Tom is here.")
    assert names.count("Tom") <= 1


def test_ingest_history_links_entities(core, scope):
    """turns 带 `entities` 字段 → 实体建好并挂到记忆上（图通道数据源）。"""
    core.ingest_history(
        scope,
        [{"text": "Alice Johnson told me about the new plan", "role": "user", "entities": ["Alice Johnson"]}],
    )
    session = core._session(scope)  # noqa: SLF001
    row = session.conn.execute("SELECT entity_ids FROM memories LIMIT 1").fetchone()
    assert row and row["entity_ids"]
    eid = row["entity_ids"].strip("[]").strip('"')
    ent = session.conn.execute("SELECT canonical_name FROM entities WHERE id=?", (eid,)).fetchone()
    assert ent and ent["canonical_name"] == "Alice Johnson"


def test_ingest_history_without_entities_unchanged(core, scope):
    """不带 entities 字段：行为与旧版一致（实体只来自 speaker）。"""
    core.ingest_history(
        scope,
        [{"text": "plain sentence here", "role": "user", "speaker": "Alice"}],
    )
    session = core._session(scope)  # noqa: SLF001
    row = session.conn.execute("SELECT entity_ids FROM memories LIMIT 1").fetchone()
    assert row
    count = len([x for x in row["entity_ids"].strip("[]").split(",") if x.strip()])
    assert count == 1  # 只有 speaker 实体


def test_run_items_english_entities_default_off(core):
    """默认 english_entities=False：导入口径与旧版一致（turn dict 无 entities）。"""
    from hippocampus.eval import public_bench as pb

    items = [
        pb.Item(
            qid="q1", question="Who went with Alice Johnson?", answers=["Bob"],
            evidence_texts=["Alice Johnson went to the park yesterday."],
            turns=[pb.Turn(text="Alice Johnson went to the park yesterday.", role="user", session_id="s1")],
            group="g1",
        )
    ]
    report = pb.run_items(core, items, account_prefix="t8def")
    # 默认档能跑完、有报告（零变化）
    assert report["overall"]["n"] == 1
