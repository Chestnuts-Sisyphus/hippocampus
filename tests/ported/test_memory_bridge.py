# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.memory_bridge.py 单元测试（P1 coverage 补测）。

纯函数 + 内存 sqlite。MemorySession / get_bridge 会写账户目录，
必须 monkeypatch get_account_data_dir 到 tmp_path——绝不碰真实库。
"""

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import memory_bridge as mb

# ── 请求文本 / 三流判定 ───────────────────────────────────────────────────


def test_content_to_text():
    assert mb._content_to_text("hello") == "hello"
    assert mb._content_to_text([{"text": "a"}, "b", {"text": "c"}]) == "abc"
    assert mb._content_to_text(None) == ""
    assert mb._content_to_text(1) == ""


def test_extract_user_text_chat_and_responses():
    assert mb.extract_user_text("bad") == ""
    assert mb.extract_user_text({}) == ""
    assert mb.extract_user_text({"messages": [{"role": "user", "content": "hi"}]}) == "hi"
    assert (
        mb.extract_user_text(
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "答"},
                    {"role": "user", "content": [{"type": "text", "text": "第二问"}]},
                ]
            }
        )
        == "第二问"
    )
    assert mb.extract_user_text({"input": "直接字符串"}) == "直接字符串"
    assert (
        mb.extract_user_text(
            {
                "input": [
                    {"type": "message", "role": "user", "content": "旧"},
                    {"type": "message", "content": [{"text": "新"}]},
                ]
            }
        )
        == "新"
    )
    assert mb.extract_user_text({"input": []}) == ""
    assert mb.extract_user_text({"messages": [{"role": "assistant", "content": "x"}]}) == ""


def test_detect_flow():
    assert mb.detect_flow("x") == ""
    assert mb.detect_flow({}) == ""
    assert mb.detect_flow({"input": "hi"}) == "first"
    assert mb.detect_flow({"input": []}) == ""
    assert mb.detect_flow({"input": [{"type": "function_call", "name": "f"}]}) == "auto"
    assert mb.detect_flow({"input": [{"type": "function_call_output", "output": "1"}]}) == "auto"
    assert mb.detect_flow({"input": [{"type": "message", "role": "assistant", "content": "a"}]}) == "auto"
    assert mb.detect_flow(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "c"}],
                }
            ]
        }
    ) == "auto"
    assert mb.detect_flow({"input": [{"type": "message", "role": "user", "content": "q"}]}) == "first"
    assert (
        mb.detect_flow(
            {
                "input": [
                    {"type": "message", "role": "user", "content": "q1"},
                    {"type": "message", "role": "assistant", "content": "a"},
                    {"type": "message", "role": "user", "content": "q2"},
                ]
            }
        )
        == "user"
    )

    assert mb.detect_flow({"messages": []}) == ""
    assert mb.detect_flow({"messages": [{"role": "assistant", "content": "a"}]}) == "auto"
    assert mb.detect_flow({"messages": [{"role": "tool", "content": "t"}]}) == "auto"
    assert mb.detect_flow(
        {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c"}]}]}
    ) == "auto"
    assert mb.detect_flow({"messages": [{"role": "user", "content": "q"}]}) == "first"
    assert mb.detect_flow(
        {
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "q2"},
            ]
        }
    ) == "user"


def test_request_body_text():
    assert mb.request_body_text("x") == ""
    t = mb.request_body_text(
        {
            "input": "hello",
            "instructions": "sys",
            "system": "chat-sys",
        }
    )
    assert "hello" in t and "sys" in t
    t = mb.request_body_text(
        {
            "input": [
                {"type": "message", "content": "m"},
                {"type": "function_call", "arguments": '{"a":1}'},
                {"type": "function_call_output", "output": "2"},
            ]
        }
    )
    assert "m" in t and "a" in t and "2" in t
    t = mb.request_body_text(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": "c",
                    "tool_calls": [{"function": {"arguments": '{"k":1}'}}],
                }
            ],
            "system": [{"text": "块sys"}],
        }
    )
    assert "c" in t and "k" in t and "块sys" in t


# ── alarm / 稳定层 / 去重（内存库，不碰真实数据）──────────────────────────


def test_stash_and_take_alarm():
    sess = SimpleNamespace(_pending_alarm=None)
    # 复用真实方法
    mb.MemorySession.stash_alarm(sess, "问", "警报")
    assert mb.MemorySession.take_alarm(sess, "问") == "警报"
    assert mb.MemorySession.take_alarm(sess, "问") == ""  # 已取走
    mb.MemorySession.stash_alarm(sess, "问", "警报2")
    assert mb.MemorySession.take_alarm(sess, "另一问") == ""  # 不匹配不消费


@pytest.fixture()
def mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.commit()
    yield c
    c.close()


def test_stable_layer_disabled_and_identity(mem_conn, monkeypatch):
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.get_active_params", lambda _c: {"stable_layer_enabled": False})
    text, ids = mb.stable_layer(mem_conn, "任何")
    assert text == "" and ids == []

    monkeypatch.setattr(
        "hippocampus.memory.memory_bridge.rt.get_active_params",
        lambda _c: {
            "stable_layer_enabled": True,
            "identity_declaration_enabled": True,
            "theme_layer_max": 6,
        },
    )
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.extract_query_entities", lambda *_a, **_k: [])
    text, ids = mb.stable_layer(mem_conn, "你好")
    assert mb.IDENTITY_DECLARATION in text
    assert "仅供参考" in text
    assert ids == []


def test_stable_layer_theme_memories(mem_conn, monkeypatch):
    eid = db.add_entity(mem_conn, "Python", "Abstract")
    mid = db.add_memory(mem_conn, "fact", "Python 是解释型语言", entity_ids=[eid], source_quote="原话")
    mem_conn.commit()
    monkeypatch.setattr(
        "hippocampus.memory.memory_bridge.rt.get_active_params",
        lambda _c: {
            "stable_layer_enabled": True,
            "identity_declaration_enabled": False,
            "theme_layer_max": 6,
        },
    )
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.extract_query_entities", lambda *_a, **_k: ["Python"])
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.resolve_entities", lambda _c, _n: [eid])
    text, ids = mb.stable_layer(mem_conn, "说说 Python")
    assert mid in ids
    assert "解释型语言" in text
    assert "原话" in text


def test_dedup_results(mem_conn):
    eid = db.add_entity(mem_conn, "X", "Concrete")
    mid = db.add_memory(mem_conn, "fact", "这段会出现在请求体", entity_ids=[eid], source_quote="引用片段")
    epid = db.add_episode(mem_conn, "s1", "user", "经历正文也会在请求体")
    mem_conn.commit()
    results = [
        {"doc_id": "stable-1", "kind": "memory"},
        {"doc_id": mid, "kind": "memory"},
        {"doc_id": epid, "kind": "episode"},
        {"doc_id": "keep-me", "kind": "memory"},
    ]
    out = mb._dedup_results(mem_conn, results, "前缀 这段会出现在请求体 经历正文也会在请求体 后缀", ["stable-1"])
    assert [r["doc_id"] for r in out] == ["keep-me"]
    # 空基准原样返回
    assert mb._dedup_results(mem_conn, results, "", []) == results


def test_handle_confirmation_none_and_hit(monkeypatch):
    sess = SimpleNamespace(
        lock=threading.Lock(),
        pending_blocks=[],
        conn=None,
        collections=None,
        reindex=lambda: None,
        match_confirmation=lambda text: None,
        pop_pending_block=lambda block: sess.pending_blocks.remove(block),
    )
    assert mb.handle_confirmation(sess, "") is None
    assert mb.handle_confirmation(sess, "确认") is None

    class Block:
        pass

    block = Block()
    sess.pending_blocks = [block]
    sess.match_confirmation = lambda text: (block, "yes") if text == "好的" else None
    monkeypatch.setattr("hippocampus.memory.memory_bridge.confirm.apply_confirmation", lambda *_a, **_k: "已写入")
    msg = mb.handle_confirmation(sess, "好的")
    assert msg.startswith("> 记忆·确认：")
    assert sess.pending_blocks == []

    sess.pending_blocks = [Block()]

    def _boom(*_a, **_k):
        raise RuntimeError("落库失败")

    sess.match_confirmation = lambda text: (sess.pending_blocks[0], "yes")
    monkeypatch.setattr("hippocampus.memory.memory_bridge.confirm.apply_confirmation", _boom)
    assert mb.handle_confirmation(sess, "好的") is None  # 软失败


def test_get_bridge_isolated(tmp_path, monkeypatch):
    """get_bridge / drop_bridge 隔离到临时目录；chroma/BM25 mock，避免加载模型。"""
    from hippocampus.memory import account

    class _FakeClient:
        def get_or_create_collection(self, *_a, **_k):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(account, "get_account_data_dir", lambda aid: tmp_path / aid)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.chromadb.PersistentClient", lambda path: _FakeClient())
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt._resolve_embedding_function", lambda _m: None)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.build_bm25", lambda _c: None)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.sync_index", lambda *_a, **_k: None)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.fb.seed_default_params", lambda _c: None)
    aid = "unit-test-bridge"
    mb.drop_bridge(aid)
    try:
        sess = mb.get_bridge(aid)
        assert sess.account_id == aid
        assert (tmp_path / aid / "memory.db").exists()
        assert mb.get_bridge(aid) is sess
        sess.reindex()
        sess.close()
    finally:
        mb.drop_bridge(aid)
        mb.drop_bridge("nonexistent")  # 幂等


def test_get_bridge_drains_embeddings_queue(tmp_path, monkeypatch):
    """发现 31 → B6 口径变更：会话初始化**不再手工清** embeddings_queue，只做源真相全量同步。"""
    import sqlite3

    from hippocampus.memory import account
    from hippocampus.memory import retrieval as rt

    class _FakeClient:
        def get_or_create_collection(self, *_a, **_k):
            return object()

        def close(self):
            pass

    aid = "unit-drain-queue"
    chroma_dir = tmp_path / aid / "chroma"
    chroma_dir.mkdir(parents=True)
    db_path = chroma_dir / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings_queue (seq_id INTEGER)")
    conn.execute("INSERT INTO embeddings_queue VALUES (1),(2),(3)")
    conn.commit()
    conn.close()
    assert rt.embeddings_queue_depth(chroma_dir) == 3

    monkeypatch.setattr(account, "get_account_data_dir", lambda x: tmp_path / x)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.chromadb.PersistentClient", lambda path: _FakeClient())
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt._resolve_embedding_function", lambda _m: None)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.build_bm25", lambda _c: None)
    _sync_calls = {"n": 0}

    def _count_sync(*_a, **_k):
        _sync_calls["n"] += 1

    monkeypatch.setattr("hippocampus.memory.memory_bridge.rt.sync_index", _count_sync)
    monkeypatch.setattr("hippocampus.memory.memory_bridge.fb.seed_default_params", lambda _c: None)
    mb.drop_bridge(aid)
    try:
        mb.get_bridge(aid)
        # [HIPPO] 口径变更（B6 根因处理）：会话初始化**不再手工删** chroma 的
        # embeddings_queue（改内部表会把"还没落段的写入"一起抹掉，实测造成
        # "Nothing found on disk" 的间歇读失败）。新契约＝队列非空时做一次
        # **源真相全量 upsert** ＋ 打开 chroma 自己的 automatically_purge；
        # 行数不减（数据不丢），回收交给 chroma。
        assert rt.embeddings_queue_depth(chroma_dir) == 3, "不得再手工清 chroma 的队列行"
        # 该走的那一步（幂等全量同步）确实被调到了：sync_index 在这里被替身计数
        assert _sync_calls["n"] >= 1, "队列非空时应做一次源真相全量同步"
    finally:
        mb.drop_bridge(aid)
