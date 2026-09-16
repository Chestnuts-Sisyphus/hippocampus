# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.memory.extract.py 单元测试（P1 coverage 补测）。

LLM 一律 mock（monkeypatch chat_json）——不碰真实 key / 真实库。
"""

import pytest

from hippocampus.memory import extract


def test_dynamic_max_tokens():
    assert extract._dynamic_max_tokens("") == 2000
    assert extract._dynamic_max_tokens("x" * 100) == 2050
    assert extract._dynamic_max_tokens("x" * 20000) == extract._MAX_OUTPUT_TOKENS


def test_split_chunks_short_and_empty():
    assert extract._split_chunks("hello") == ["hello"]
    # 全空白：split 后无段落，兜底 [text]
    assert extract._split_chunks("\n\n") == ["\n\n"]


def test_split_chunks_by_paragraph_and_hard_cut():
    paras = [f"段{i} " + ("字" * 50) for i in range(50)]
    text = "\n".join(paras)
    chunks = extract._split_chunks(text)
    assert len(chunks) >= 2
    assert all(len(c) <= extract._CHUNK_MAX_CHARS or c == chunks[-1] for c in chunks[:-1])
    # 单段超长硬切
    huge = "字" * (extract._CHUNK_MAX_CHARS + 100)
    chunks = extract._split_chunks(huge)
    assert len(chunks) >= 2
    assert chunks[0] == huge[: extract._CHUNK_MAX_CHARS]


def test_halve_prefers_newline():
    text = "aaaa\nbbbb"
    a, b = extract._halve(text)
    assert a and b
    # 无换行：按中点切
    a, b = extract._halve("abcdefgh")
    assert a + b == "abcdefgh"
    # 一侧为空时回退中点
    a, b = extract._halve("\nxxxx")
    assert a and b


def test_chat_json_with_retry_success_and_retry(monkeypatch):
    monkeypatch.setattr(extract, "chat_json", lambda *a, **k: {"entities": [], "memories": []})
    assert extract._chat_json_with_retry("s", "u", 100) == {"entities": [], "memories": []}

    calls = {"n": 0}

    def _flaky(system, user, max_tokens=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("截断")
        return {"ok": True, "mt": max_tokens}

    monkeypatch.setattr(extract, "chat_json", _flaky)
    r = extract._chat_json_with_retry("s", "u", 1000)
    assert r["ok"] is True
    assert r["mt"] == 1500
    assert calls["n"] == 2


def test_extract_block_success_and_subsplit(monkeypatch):
    monkeypatch.setattr(extract, "chat_json", lambda *a, **k: {"entities": [{"name": "A"}], "memories": []})
    r = extract._extract_block("短文本")
    assert r["entities"][0]["name"] == "A"

    def _fail_if_long(system, user, max_tokens=0):
        # 长块失败 → 二次细分；短块成功
        if len(user) > extract._SUBSPLIT_MIN:
            raise RuntimeError("爆炸")
        return {"entities": [{"name": "子"}], "memories": [{"type": "fact", "content": "x"}]}

    monkeypatch.setattr(extract, "chat_json", _fail_if_long)
    text = "第一段内容足够长用来对半切\n" + ("x" * 400) + "\n第二段也要够长\n" + ("y" * 400)
    r = extract._extract_block(text)
    names = [e["name"] for e in r["entities"]]
    assert names == ["子"]  # 同名去重
    assert r["memories"]


def test_extract_block_too_short_reraises(monkeypatch):
    def _always_fail(*a, **k):
        raise RuntimeError("挂")

    monkeypatch.setattr(extract, "chat_json", _always_fail)
    with pytest.raises(RuntimeError, match="挂"):
        extract._extract_block("短")  # <= _SUBSPLIT_MIN


def test_extract_short_and_long(monkeypatch):
    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda *a, **k: {"entities": [{"name": "Hippocampus", "type": "Abstract"}], "memories": []},
    )
    r = extract.extract("我不喜欢打补丁")
    assert r["entities"][0]["name"] == "Hippocampus"

    # 长文本走分块 + 已知实体名提示
    n = {"i": 0}

    def _chunked(system, user, max_tokens=0):
        n["i"] += 1
        return {"entities": [{"name": f"E{n['i']}"}], "memories": [{"content": str(n["i"])}]}

    monkeypatch.setattr(extract, "chat_json", _chunked)
    long_text = ("段落内容。" * 80 + "\n") * 20  # > 2500
    assert len(long_text) > extract._CHUNK_THRESHOLD
    r = extract.extract(long_text)
    names = [e["name"] for e in r["entities"]]
    assert names  # 至少一块
    assert names == list(dict.fromkeys(names))  # 去重保序
    assert len(r["memories"]) >= 1


def test_extract_response_items(monkeypatch):
    assert extract.extract_response_items("") == {"entities": [], "memories": []}
    assert extract.extract_response_items("   ") == {"entities": [], "memories": []}

    monkeypatch.setattr(
        extract,
        "chat_json",
        lambda *a, **k: {"entities": [], "memories": [{"type": "resource", "content": "密钥在 config.yaml"}]},
    )
    r = extract.extract_response_items("密钥在 config.yaml 里")
    assert r["memories"][0]["type"] == "resource"

    calls = {"n": 0}

    def _flaky(system, user, max_tokens=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return {"entities": [], "memories": []}

    monkeypatch.setattr(extract, "chat_json", _flaky)
    r = extract.extract_response_items("这个 bug 已经修复了")
    assert r["memories"] == []
    assert calls["n"] == 2
