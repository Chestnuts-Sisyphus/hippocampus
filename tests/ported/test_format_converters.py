# 由前身 hippocampus_prototype/tests 抽取移植（只改写 import 与字符串模块路径，断言逻辑原样）。
"""hippocampus.proxy.format_converters.py 单元测试（P1 coverage 补测）。

纯函数转换：不碰真实库、不启代理。覆盖 tool_choice/tools、三组请求/响应转换、SSE。
"""

import asyncio
import json

import httpx
import pytest

from hippocampus.proxy import format_converters as fc
from hippocampus.proxy.llm_proxy import UpstreamError


def _collect(agen) -> list:
    """跑完 async generator，收集全部 yield。"""

    async def _run():
        return [x async for x in agen]

    return asyncio.run(_run())


def _anth_sse(*objs) -> list[str]:
    """构造 anthropic 风格 data: JSON 行（含收尾 message_stop）。"""
    lines = [f"data: {json.dumps(o, ensure_ascii=False)}" for o in objs]
    return lines


# ── SSE / 文本辅助 ────────────────────────────────────────────────────────


def test_sse_event_and_error():
    ev = fc._sse_event("message_stop", {"type": "message_stop"})
    assert ev.startswith("event: message_stop\n")
    assert "message_stop" in ev
    err = fc._sse_error("挂了")
    assert "error" in err
    assert "挂了" in err


def test_extract_text_variants():
    assert fc._extract_text("hello") == "hello"
    assert fc._extract_text([{"type": "text", "text": "a"}, {"text": "b"}]) == "ab"
    assert fc._extract_text(["x", {"text": "y"}]) == "xy"
    assert fc._extract_text(None) == ""
    assert fc._extract_text(123) == ""


# ── tool_choice / tools ───────────────────────────────────────────────────


def test_tool_choice_chat_to_anthropic():
    assert fc.tool_choice_chat_to_anthropic(None) is None
    assert fc.tool_choice_chat_to_anthropic("auto") == {"type": "auto"}
    assert fc.tool_choice_chat_to_anthropic("none") is None
    assert fc.tool_choice_chat_to_anthropic("required") == {"type": "any"}
    assert fc.tool_choice_chat_to_anthropic("unknown") == {"type": "auto"}
    assert fc.tool_choice_chat_to_anthropic({"type": "function", "function": {"name": "fn"}}) == {
        "type": "tool",
        "name": "fn",
    }
    assert fc.tool_choice_chat_to_anthropic({"type": "any"}) == {"type": "any"}
    assert fc.tool_choice_chat_to_anthropic({"type": "auto"}) == {"type": "auto"}
    assert fc.tool_choice_chat_to_anthropic({"type": "other"}) == {"type": "auto"}
    assert fc.tool_choice_chat_to_anthropic(1) == {"type": "auto"}


def test_tool_choice_anthropic_to_chat():
    assert fc.tool_choice_anthropic_to_chat(None) == "auto"
    assert fc.tool_choice_anthropic_to_chat({"type": "auto"}) == "auto"
    assert fc.tool_choice_anthropic_to_chat({"type": "any"}) == "required"
    assert fc.tool_choice_anthropic_to_chat({"type": "tool", "name": "fn"}) == {
        "type": "function",
        "function": {"name": "fn"},
    }
    assert fc.tool_choice_anthropic_to_chat("x") == "auto"


def test_tool_choice_responses_to_anthropic():
    assert fc.tool_choice_responses_to_anthropic(None) is None
    assert fc.tool_choice_responses_to_anthropic("auto") == {"type": "auto"}
    assert fc.tool_choice_responses_to_anthropic("none") is None
    assert fc.tool_choice_responses_to_anthropic("required") == {"type": "any"}
    assert fc.tool_choice_responses_to_anthropic("zzz") == {"type": "auto"}
    assert fc.tool_choice_responses_to_anthropic({"type": "function", "name": "fn"}) == {"type": "tool", "name": "fn"}
    assert fc.tool_choice_responses_to_anthropic({"type": "any"}) == {"type": "any"}
    assert fc.tool_choice_responses_to_anthropic(0) == {"type": "auto"}


def test_tools_converters():
    chat_tools = [
        {
            "type": "function",
            "function": {"name": "add", "description": "加", "parameters": {"type": "object"}},
        },
        "skip-me",
    ]
    anth = fc.tools_chat_to_anthropic(chat_tools)
    assert anth == [{"name": "add", "description": "加", "input_schema": {"type": "object"}}]
    back = fc.tools_anthropic_to_chat(anth)
    assert back[0]["function"]["name"] == "add"
    assert fc.tools_chat_to_anthropic(None) == []
    assert fc.tools_anthropic_to_chat(None) == []

    resp_tools = [{"type": "function", "name": "g", "description": "d", "parameters": {"type": "object"}}]
    anth2 = fc.tools_responses_to_anthropic(resp_tools)
    assert anth2[0]["name"] == "g"
    # 无 type=function 但有 name 也收
    anth3 = fc.tools_responses_to_anthropic([{"name": "h"}])
    assert anth3[0]["name"] == "h"


# ── cache_control / 块转换 ────────────────────────────────────────────────


def test_to_anthropic_text_blocks_passthrough():
    fc.set_cache_control_passthrough(True)
    blocks = fc._to_anthropic_text_blocks("hi", True)
    assert blocks == [{"type": "text", "text": "hi"}]
    blocks = fc._to_anthropic_text_blocks(
        [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}, "b"],
        True,
    )
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1] == {"type": "text", "text": "b"}
    stripped = fc._to_anthropic_text_blocks(
        [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        False,
    )
    assert "cache_control" not in stripped[0]
    assert fc._to_anthropic_text_blocks("", True) == []
    assert fc._to_anthropic_text_blocks(None, True) == []
    fc.set_cache_control_passthrough(True)


# ── 组 1: chat <-> anthropic ──────────────────────────────────────────────


def test_chat_to_anthropic_request_basic_and_tools():
    body = {
        "model": "ignored",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"x":1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "2"},
        ],
        "tools": [{"type": "function", "function": {"name": "add", "parameters": {}}}],
        "tool_choice": "auto",
        "temperature": 0.2,
    }
    out = fc.chat_to_anthropic_request(body, "claude")
    assert out["model"] == "claude"
    assert out["max_tokens"] == 4096
    assert out["temperature"] == 0.2
    assert out["system"][0]["text"] == "你是助手"
    assert out["tools"][0]["name"] == "add"
    assert out["tool_choice"] == {"type": "auto"}
    roles = [m["role"] for m in out["messages"]]
    assert "assistant" in roles and "user" in roles
    tool_use = [b for m in out["messages"] if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]
    assert tool_use and tool_use[0]["id"] == "c1"


def test_chat_to_anthropic_request_list_content_and_bad_args():
    body = {
        "messages": [
            {"role": "developer", "content": [{"type": "text", "text": "dev"}]},
            {"role": "user", "content": [{"type": "text", "text": "问", "cache_control": {"type": "ephemeral"}}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "答"}, {"type": "tool_use", "id": "t", "name": "f", "input": {}}],
            },
            {"role": "tool", "tool_call_id": "t", "content": [{"type": "text", "text": "结果"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "function": {"name": "f", "arguments": "{"}}]},
        ],
        "tool_choice": "none",
        "max_tokens": 16,
    }
    out = fc.chat_to_anthropic_request(body, "m")
    assert out["max_tokens"] == 16
    assert "tool_choice" not in out  # none → 不发
    assert any(b.get("cache_control") for m in out["messages"] for b in m.get("content") or [] if isinstance(b, dict))


def test_anthropic_to_chat_json_text_and_tools():
    up = {
        "id": "msg_1",
        "content": [
            {"type": "text", "text": "你好"},
            {"type": "tool_use", "id": "t1", "name": "add", "input": {"a": 1}},
            "skip",
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    out = fc.anthropic_to_chat_json(up, "gpt")
    assert out["object"] == "chat.completion"
    assert out["model"] == "gpt"
    assert out["choices"][0]["message"]["content"] == "你好"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    tc = out["choices"][0]["message"]["tool_calls"][0]
    assert tc["id"] == "t1"
    assert json.loads(tc["function"]["arguments"]) == {"a": 1}
    assert out["usage"]["total_tokens"] == 7


def test_anthropic_to_chat_sse_text_and_after_task():
    async def gen():
        yield "event: ping"
        yield ""
        yield "not-data"
        yield 'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}}'
        yield "data: {bad"
        yield 'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}}'
        yield 'data: {"type": "message_delta"}'
        yield 'data: {"type": "message_stop"}'
        yield "data: [DONE]"

    async def after():
        return "确认"

    seen = []
    chunks = _collect(fc.anthropic_to_chat_sse(gen(), "m", after_task=after(), on_complete=seen.append))
    joined = "".join(chunks)
    assert "你" in joined and "好" in joined
    assert "确认" in joined
    assert "data: [DONE]" in joined
    assert seen == ["你好"]


def test_anthropic_to_chat_sse_tool_use_and_upstream_error():
    async def gen_tool():
        yield (
            'data: {"type": "content_block_start", "index": 0, '
            '"content_block": {"type": "tool_use", "id": "c1", "name": "add"}}'
        )
        yield (
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "input_json_delta", "partial_json": "{\\"x\\":"}}'
        )
        yield 'data: {"type": "message_stop"}'

    chunks = _collect(fc.anthropic_to_chat_sse(gen_tool(), "m"))
    joined = "".join(chunks)
    assert "tool_calls" in joined
    assert "add" in joined
    assert "[DONE]" in joined

    async def gen_err():
        if False:
            yield "unused"
        raise UpstreamError(500, "boom")

    chunks = _collect(fc.anthropic_to_chat_sse(gen_err(), "m"))
    assert any("[DONE]" in c for c in chunks)

    async def gen_http():
        if False:
            yield "unused"
        raise httpx.ConnectError("nope")

    chunks = _collect(fc.anthropic_to_chat_sse(gen_http(), "m"))
    assert any("[DONE]" in c for c in chunks)


def test_anthropic_to_chat_sse_on_complete_soft_fail():
    async def gen():
        yield 'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "a"}}'

    def boom(_t):
        raise RuntimeError("cb")

    chunks = _collect(fc.anthropic_to_chat_sse(gen(), "m", on_complete=boom))
    assert any("[DONE]" in c for c in chunks)


# ── 组 2: messages -> chat ────────────────────────────────────────────────


def test_messages_to_chat_request_roundtrip_shapes():
    body = {
        "model": "ignored",
        "system": [{"type": "text", "text": "sys"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "用工具"},
                    {"type": "tool_use", "id": "c1", "name": "add", "input": {"x": 1}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "2"},
                    {"type": "text", "text": "继续"},
                ],
            },
            {"role": "user", "content": [{"type": "text", "text": ""}]},
            {"role": "assistant", "content": 123},
        ],
        "tools": [{"name": "add", "description": "", "input_schema": {}}],
        "tool_choice": {"type": "any"},
        "max_tokens": 8,
        "temperature": 0.1,
    }
    out = fc.messages_to_chat_request(body, "gpt")
    assert out["model"] == "gpt"
    assert out["max_tokens"] == 8
    assert out["tool_choice"] == "required"
    assert out["messages"][0]["role"] == "system"
    assert any(m.get("role") == "tool" for m in out["messages"])
    assert any(m.get("tool_calls") for m in out["messages"] if m.get("role") == "assistant")

    body2 = {"system": "纯字符串", "messages": [{"role": "user", "content": "q"}]}
    out2 = fc.messages_to_chat_request(body2, "m")
    assert out2["messages"][0]["content"] == "纯字符串"


def test_chat_to_anthropic_json():
    up = {
        "choices": [
            {
                "message": {
                    "content": "hi",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "add", "arguments": '{"a":1}'}},
                        {"id": "c2", "function": {"name": "bad", "arguments": "{"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }
    out = fc.chat_to_anthropic_json(up, "claude")
    assert out["type"] == "message"
    types = [b["type"] for b in out["content"]]
    assert "text" in types and "tool_use" in types
    assert out["stop_reason"] == "tool_use"
    empty = fc.chat_to_anthropic_json({}, "m")
    assert empty["content"] == [{"type": "text", "text": ""}]


def test_chat_to_anthropic_sse_text_tools_and_errors():
    async def gen():
        yield 'data: {"choices": [{"delta": {"content": "你"}}]}'
        yield "data: {bad"
        yield 'data: {"choices": [{"delta": {"content": "好"}}], "usage": {"completion_tokens": 2}}'
        yield (
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", '
            '"function": {"name": "add", "arguments": "{\\"x\\":1}"}}]}}]}'
        )
        yield "data: [DONE]"

    seen = []

    async def after():
        return "尾"

    chunks = _collect(fc.chat_to_anthropic_sse(gen(), "m", after_task=after(), on_complete=seen.append))
    joined = "".join(chunks)
    assert "text_delta" in joined
    assert "tool_use" in joined
    assert "尾" in joined
    assert "message_stop" in joined
    assert seen == ["你好"]

    async def gen_err():
        if False:
            yield "unused"
        raise UpstreamError(502, "x" * 300)

    chunks = _collect(fc.chat_to_anthropic_sse(gen_err(), "m"))
    assert any("error" in c for c in chunks)

    async def gen_http():
        if False:
            yield "unused"
        raise httpx.ReadTimeout("t")

    chunks = _collect(fc.chat_to_anthropic_sse(gen_http(), "m"))
    assert any("error" in c for c in chunks)


def test_chat_to_anthropic_sse_after_only_and_cb_fail():
    async def gen():
        yield 'data: {"choices": []}'
        yield "data: [DONE]"

    async def after():
        return "只有确认"

    chunks = _collect(fc.chat_to_anthropic_sse(gen(), "m", after_task=after()))
    joined = "".join(chunks)
    assert "只有确认" in joined

    async def gen2():
        yield 'data: {"choices": [{"delta": {"content": "a"}}]}'

    def boom(_t):
        raise RuntimeError("x")

    chunks = _collect(fc.chat_to_anthropic_sse(gen2(), "m", on_complete=boom))
    assert any("[DONE]" in c for c in chunks)


# ── 组 3: responses <-> anthropic ─────────────────────────────────────────


def test_responses_to_anthropic_request_str_and_list():
    out = fc.responses_to_anthropic_request(
        {"instructions": "sys", "input": "hello", "temperature": 0.5, "max_tokens": 10},
        "claude",
    )
    assert out["system"][0]["text"] == "sys"
    assert out["messages"][0]["role"] == "user"
    assert out["max_tokens"] == 10

    body = {
        "input": [
            {"type": "message", "role": "developer", "content": "dev-sys"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "问"}]},
            {"type": "function_call", "call_id": "c1", "name": "add", "arguments": '{"x":1}'},
            {"type": "function_call_output", "call_id": "c1", "output": "2"},
            {"type": "function_call", "name": "b", "arguments": {"y": 2}},
            {"type": "function_call_output", "call_id": "c2", "output": {"ok": True}},
            "skip",
        ],
        "tools": [{"type": "function", "name": "add", "parameters": {}}],
        "tool_choice": "required",
    }
    out = fc.responses_to_anthropic_request(body, "m")
    assert out["tool_choice"] == {"type": "any"}
    assert any(b.get("type") == "tool_use" for m in out["messages"] for b in m["content"])
    assert any(b.get("type") == "tool_result" for m in out["messages"] for b in m["content"])


def test_responses_to_anthropic_request_errors():
    with pytest.raises(ValueError, match="缺失"):
        fc.responses_to_anthropic_request({}, "m")
    with pytest.raises(ValueError, match="类型不支持"):
        fc.responses_to_anthropic_request({"input": 123}, "m")
    with pytest.raises(ValueError, match="无有效消息"):
        fc.responses_to_anthropic_request({"input": []}, "m")
    with pytest.raises(ValueError, match="无有效消息"):
        fc.responses_to_anthropic_request({"input": "   "}, "m")


def test_anthropic_to_responses_json():
    up = {
        "content": [
            {"type": "text", "text": "答案"},
            {"type": "tool_use", "id": "c1", "name": "add", "input": {"a": 1}},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    out = fc.anthropic_to_responses_json(up, "m")
    assert out["object"] == "response"
    kinds = [o["type"] for o in out["output"]]
    assert "message" in kinds and "function_call" in kinds
    assert out["usage"]["total_tokens"] == 3


def test_anthropic_to_responses_sse_text_tool_and_errors():
    async def gen():
        yield "event: ping"
        yield ""
        yield "not-data"
        yield 'data: {"type": "message_start", "message": {"usage": {"input_tokens": 4}}}'
        yield 'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}}'
        yield "data: {bad"
        yield (
            'data: {"type": "content_block_start", "index": 1, '
            '"content_block": {"type": "tool_use", "id": "c1", "name": "add"}}'
        )
        yield (
            'data: {"type": "content_block_delta", "index": 1, '
            '"delta": {"type": "input_json_delta", "partial_json": "{}"}}'
        )
        yield 'data: {"type": "message_stop"}'
        yield "data: [DONE]"

    seen = []

    async def after():
        return "尾"

    chunks = _collect(fc.anthropic_to_responses_sse(gen(), "m", after_task=after(), on_complete=seen.append))
    joined = "".join(chunks)
    assert "response.created" in joined
    assert "response.output_text.delta" in joined
    assert "function_call" in joined
    assert "尾" in joined
    assert "[DONE]" in joined
    assert seen == ["你"]

    async def gen_err():
        if False:
            yield "unused"
        raise UpstreamError(500, "e")

    chunks = _collect(fc.anthropic_to_responses_sse(gen_err(), "m"))
    assert any("error" in c or "[DONE]" in c for c in chunks)

    async def gen_http():
        if False:
            yield "unused"
        raise httpx.ConnectError("n")

    chunks = _collect(fc.anthropic_to_responses_sse(gen_http(), "m"))
    assert any("error" in c or "[DONE]" in c for c in chunks)
