"""N6 真流式转发（A1）：上游 SSE 逐行转，三格式各一条测试；流末固化＋确认块末尾 delta。

验收口径（缺口清单 N6）：
- 上游 SSE 逐行转发：mock 上游（chat／anthropic／responses 三种）→ 客户端**逐行**收到
  （多个 delta 事件，不是整段一次到达）；
- 流结束后仍完成固化（consolidate 落库）；
- 确认块作为末尾 delta 追加（有冲突时）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hippocampus.proxy.app import build_app


def _chat_upstream(lines: list[str]):
    """构造 chat 上游的 mock：stream=True 返回逐行 async 生成器。"""

    def _upstream(payload, *, model="m", endpoint="chat", stream=False, body=None):
        if not stream:
            return {"choices": [{"message": {"role": "assistant", "content": ""}}], "usage": {}}, {}
        async def _gen():
            for line in lines:
                yield line + "\n"
            yield "data: [DONE]\n\n"

        return _gen()

    return _upstream


def _chat_delta(text: str) -> str:
    return "data: " + json.dumps(
        {"id": "x", "object": "chat.completion.chunk", "model": "m",
         "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        ensure_ascii=False,
    )


def _anthropic_upstream(deltas: list[str]):
    def _upstream(payload, *, model="m", endpoint="anthropic", stream=False, body=None):
        if not stream:
            return {"content": [{"type": "text", "text": ""}], "usage": {}}, {}
        async def _gen():
            # 逐行 yield（与 llm_proxy.stream_upstream 的 aiter_lines 形状一致）
            yield "event: message_start\n"
            yield 'data: {"type":"message_start","message":{}}\n'
            yield "\n"
            yield "event: content_block_start\n"
            yield 'data: {"type":"content_block_start","index":0}\n'
            yield "\n"
            for d in deltas:
                yield "event: content_block_delta\n"
                yield "data: " + json.dumps(
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": d}}, ensure_ascii=False) + "\n"
                yield "\n"
            yield "event: message_stop\n"
            yield 'data: {"type":"message_stop"}\n'
            yield "\n"

        return _gen()

    return _upstream


def _responses_upstream(deltas: list[str]):
    def _upstream(payload, *, model="m", endpoint="responses", stream=False, body=None):
        if not stream:
            return {"output": [{"content": [{"text": ""}]}], "usage": {}}, {}
        async def _gen():
            yield "event: response.created\n"
            yield 'data: {"type":"response.created","response":{}}\n'
            yield "\n"
            for d in deltas:
                yield "event: response.output_text.delta\n"
                yield "data: " + json.dumps(
                    {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                     "delta": d}, ensure_ascii=False) + "\n"
                yield "\n"
            yield "event: response.completed\n"
            yield 'data: {"type":"response.completed"}\n'
            yield "\n"
            yield "data: [DONE]\n"
            yield "\n"

        return _gen()

    return _upstream


def _collect_chat_lines(client: TestClient, path: str, body: dict, headers: dict | None = None) -> list[str]:
    with client.stream("POST", path, json=body, headers=headers or {}) as resp:
        assert resp.status_code == 200
        return [l for l in resp.iter_lines() if l.strip()]


def test_streaming_chat_line_by_line(core, scope):
    """chat 入站：上游两条 delta → 客户端逐行收到（不是整段一次到达）＋流末固化。"""
    app = build_app(core, confirm_block=True, offline=False, upstream=_chat_upstream(
        [_chat_delta("你"), _chat_delta("好")]
    ))
    with TestClient(app) as client:
        lines = _collect_chat_lines(
            client, "/v1/chat/completions",
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "打个招呼"}]},
            headers={"X-Hippocampus-Account": scope.account},
        )
    data_lines = [l for l in lines if l.startswith("data:") and "[DONE]" not in l]
    contents = []
    for l in data_lines:
        obj = json.loads(l[5:])
        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
        if delta.get("content"):
            contents.append(delta["content"])
    assert contents == ["你", "好"], f"应逐行收到两个 delta，实际 {contents}"
    # 流末固化：用户轮进库（经历至少一条）
    assert core.stats(scope)["episodes"] >= 1


def test_streaming_anthropic_line_by_line(core, scope, monkeypatch):
    """anthropic 入站：上游两条 content_block_delta → 客户端逐行收到。"""
    import hippocampus.settings as settings

    monkeypatch.setattr(settings, "upstream_endpoint", lambda: "anthropic")
    app = build_app(core, confirm_block=True, offline=False, upstream=_anthropic_upstream(["你", "好"]))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/messages",
            json={"model": "m", "max_tokens": 100, "stream": True, "system": "你是助手",
                  "messages": [{"role": "user", "content": "打个招呼"}]},
        ) as resp:
            assert resp.status_code == 200
            lines = [l for l in resp.iter_lines() if l.strip()]
    deltas = [l for l in lines if l.startswith("event: content_block_delta")]
    assert len(deltas) == 2, f"应有两个 content_block_delta 事件，实际 {len(deltas)}"
    texts = []
    for l in lines:
        if l.startswith("data:") and "content_block_delta" in l:
            texts.append(json.loads(l[5:])["delta"]["text"])
    assert texts == ["你", "好"]


def test_streaming_responses_line_by_line(core, scope, monkeypatch):
    """responses 入站：上游两条 output_text.delta → 客户端逐行收到。"""
    import hippocampus.settings as settings

    monkeypatch.setattr(settings, "upstream_endpoint", lambda: "responses")
    app = build_app(core, confirm_block=True, offline=False, upstream=_responses_upstream(["你", "好"]))
    with TestClient(app) as client:
        with client.stream(
            "POST", "/v1/responses",
            json={"model": "m", "instructions": "你是助手", "stream": True, "input": "打个招呼"},
        ) as resp:
            assert resp.status_code == 200
            lines = [l for l in resp.iter_lines() if l.strip()]
    delta_events = [l for l in lines if l.startswith("event: response.output_text.delta")]
    assert len(delta_events) == 2, f"应有两个 output_text.delta 事件，实际 {len(delta_events)}"


def test_offline_responses_reply_has_text(core, scope):
    """离线档 responses 入站：回执必须包含正文（曾只回 {"model": ...}，把正文丢了）。"""
    app = build_app(core, confirm_block=False, offline=True, upstream=None)
    with TestClient(app) as client:
        r = client.post(
            "/v1/responses",
            json={"model": "m", "instructions": "你是助手", "input": "我投简历有什么要求？"},
        )
        assert r.status_code == 200
        body = r.json()
        texts = [
            block.get("text", "")
            for item in body.get("output", [])
            for block in (item.get("content") or [])
        ]
        assert any("离线档" in t for t in texts), f"离线回执应含正文，实际 {body}"


def test_streaming_confirm_block_as_tail_delta(core, scope):
    """确认块作为末尾 delta 追加（冲突时流尾出现确认块文本）。"""
    # 造冲突：同一对象两个取值 → consolidate 后挂起确认
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户原话", explicit=True)
    app = build_app(core, confirm_block=True, offline=False, upstream=_chat_upstream(
        [_chat_delta("好的")]
    ))
    with TestClient(app) as client:
        lines = _collect_chat_lines(
            client, "/v1/chat/completions",
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "我的期望城市是北京"}]},
            headers={"X-Hippocampus-Account": scope.account},
        )
    tail = "".join(lines)
    assert "记忆·确认" in tail, "冲突时确认块应作为末尾 delta 追加"
    # 确认块确实在数据流里（被发给了客户端）
    data_parts = [l for l in lines if l.startswith("data:") and "记忆·确认" in l]
    assert data_parts, "确认块文本应出现在 data 事件里"
