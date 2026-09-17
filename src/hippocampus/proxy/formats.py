"""代理形态的**格式层**：三种入站格式的识别、注入与互相转换。

前身的代理支持 **3×3 的格式矩阵**（入站 × 上游，由 `api_mode` 决定上游）：

| 入站（客户端打过来的） | 识别依据 | 上游 chat | 上游 anthropic | 上游 responses |
|---|---|---|---|---|
| OpenAI Chat Completions | `messages` 且无 `system` 字段 | 透传 | `chat_to_anthropic_request` | 501（前身同样不支持） |
| OpenAI Responses | 有 `input` | `responses_to_chat_body` | `responses_to_anthropic_request` | 透传 |
| Anthropic Messages | 有 `system` 且 `messages` | `messages_to_chat_request` | 透传 | 501 |

响应方向对称；记忆注入**按入站格式**落在各自的位置（见 `apply_injection`）。

移植说明：转换函数在 `format_converters.py`（前身原样 vendoring，含 21 条随迁测试）；
本文件里的 `responses_to_chat_body` / `chat_json_to_responses` / `apply_injection` /
`anthropic_sse` 由前身 `proxy_app.py` 抽出——**逻辑原样**，只做了"不做 HTTP 耦合"的整理：
它们只吃 dict、只吐 dict，不认识请求对象，便于单测与复用。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

from hippocampus.proxy.format_converters import (
    _extract_text,
    _sse_event,
    anthropic_to_chat_json,
    anthropic_to_responses_json,
    chat_to_anthropic_json,
    chat_to_anthropic_request,
    messages_to_chat_request,
    responses_to_anthropic_request,
)

INBOUND_CHAT = "chat"
INBOUND_RESPONSES = "responses"
INBOUND_ANTHROPIC = "anthropic"
UPSTREAM_MODES = {"chat_completions": "chat", "anthropic_messages": "anthropic", "responses": "responses"}


class UnsupportedCombination(ValueError):
    """入站格式 → 上游格式 这一对没有转换实现（前身同样返回 501）。"""


# ----------------------------------------------------------------------
# 识别与取文本
# ----------------------------------------------------------------------


def detect_inbound(body: dict[str, Any]) -> str:
    """按请求体形状判定入站格式（三家的判别式与前身一致）。"""
    has_system = isinstance(body.get("system"), (str, list))
    if has_system and "messages" in body:
        return INBOUND_ANTHROPIC
    if "input" in body:
        return INBOUND_RESPONSES
    if "messages" in body:
        return INBOUND_CHAT
    raise ValueError("无法识别的请求格式：既没有 messages，也没有 input")


def extract_user_text(body: dict[str, Any], kind: str) -> str:
    """取"用户这一轮说的话"（用于检索与固化），格式无关地取最后一条 user 文本。"""
    if kind == INBOUND_RESPONSES:
        inp = body.get("input")
        if isinstance(inp, str):
            return inp
        if isinstance(inp, list):
            for item in reversed(inp):
                if isinstance(item, dict) and item.get("type", "message") == "message" and item.get("role", "user") == "user":
                    return _extract_text(item.get("content", ""))
        return ""
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        return _extract_text(msg.get("content", ""))
    return ""


def request_text(body: dict[str, Any], kind: str) -> str:
    """把整轮请求拼成一段文本（防"重复注入模型已见内容"用），格式无关。"""
    if kind == INBOUND_RESPONSES:
        inp = body.get("input")
        if isinstance(inp, str):
            return (body.get("instructions") or "") + "\n" + inp
        chunks = [body.get("instructions") or ""]
        for item in inp or []:
            if isinstance(item, dict):
                chunks.append(_extract_text(item.get("content", "")) or str(item.get("output") or ""))
        return "\n".join(c for c in chunks if c)
    parts = []
    if kind == INBOUND_ANTHROPIC:
        parts.append(_extract_text(body.get("system")))
    for msg in body.get("messages") or []:
        if isinstance(msg, dict):
            parts.append(_extract_text(msg.get("content", "")))
    return "\n".join(p for p in parts if p)


# ----------------------------------------------------------------------
# 记忆注入（按入站格式落位）
# ----------------------------------------------------------------------


def apply_injection(body: dict[str, Any], stable: str, fluid: str) -> dict[str, Any]:
    """两层注入（前身 `apply_injection` 原样移植，原地改 body 并返回）。

    - **chat**：stable 并进首条 system 的 content（无则插到最前）；
      fluid 追加一条 user 消息（`[海马体记忆]\\n…`）
    - **anthropic**：stable 作独立 text 块追加进 `system` 块数组（原 system 是 str 则整体作首块）；
      足够长（`len//3 >= 1024`）时挂 `cache_control: ephemeral`；fluid 追加 messages 末尾
    - **responses**：stable 拼到 `instructions` 末尾；fluid 追加 input 末尾一条 message
    """
    if not stable and not fluid:
        return body
    kind = detect_inbound(body)

    if stable:
        if kind == INBOUND_ANTHROPIC:
            sys_val = body.get("system")
            stable_block: dict[str, Any] = {"type": "text", "text": stable}
            if len(stable) // 3 >= 1024:  # 近似 token 数（Sonnet 最小可缓存 1024 token）
                stable_block["cache_control"] = {"type": "ephemeral"}
            if isinstance(sys_val, str):
                blocks = []
                if sys_val.strip():
                    blocks.append({"type": "text", "text": sys_val})
                blocks.append(stable_block)
                body["system"] = blocks
            elif isinstance(sys_val, list):
                body["system"] = list(sys_val) + [stable_block]
            else:
                body["system"] = [stable_block]
        elif kind == INBOUND_RESPONSES:
            instructions = body.get("instructions") or ""
            body["instructions"] = (instructions.rstrip() + "\n\n" + stable) if instructions else stable
        else:
            msgs = body.get("messages") or []
            if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                existing = msgs[0].get("content", "")
                if isinstance(existing, str):
                    msgs[0]["content"] = (existing.rstrip() + "\n\n" + stable) if existing else stable
                else:
                    msgs[0]["content"] = list(existing) + [{"type": "text", "text": stable}]
            else:
                msgs.insert(0, {"role": "system", "content": stable})
            body["messages"] = msgs

    if fluid:
        fluid_text = fluid if fluid.startswith("[海马体记忆]") else "[海马体记忆]\n" + fluid
        if kind == INBOUND_RESPONSES:
            inp = body.get("input")
            item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": fluid_text}]}
            if isinstance(inp, str):
                body["input"] = [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": inp}]},
                    item,
                ]
            elif isinstance(inp, list):
                body["input"] = [*inp, item]
            else:
                body["input"] = [item]
        else:
            msgs = body.get("messages") or []
            msgs.append({"role": "user", "content": fluid_text})
            body["messages"] = msgs
    return body


# ----------------------------------------------------------------------
# 请求方向：入站 → 上游
# ----------------------------------------------------------------------


def convert_tools(tools: Any) -> list[dict[str, Any]]:
    """Responses 风格 tools → chat 风格 tools（`strict` 丢弃）。"""
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" or "function" not in tool:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
        else:
            out.append(tool)
    return out


def responses_to_chat_body(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Responses 请求 → Chat 请求（前身 `_responses_to_chat_body` 原样移植）。"""
    instructions = body.get("instructions") or ""
    inp = body.get("input")
    if inp is None:
        raise ValueError("input 字段缺失")

    msgs: list[dict[str, Any]] = []
    if isinstance(inp, str):
        if inp.strip():
            msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type", "message")
            if itype == "message":
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                content = _extract_text(item.get("content", ""))
                if content:
                    msgs.append({"role": role, "content": content})
            elif itype == "function_call":
                # 保留上游原 id（随机 id 会破坏 prompt cache）
                call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}"
                arguments = item.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                msgs.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": item.get("name", ""), "arguments": arguments},
                            }
                        ],
                    }
                )
            elif itype == "function_call_output":
                call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}"
                output = item.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output)
                msgs.append({"role": "tool", "tool_call_id": call_id, "content": output})
    else:
        raise ValueError(f"input 类型不支持: {type(inp).__name__}")

    if not msgs:
        raise ValueError("input 中无有效消息")

    chat = {k: v for k, v in body.items() if k not in ("input", "instructions", "model")}
    if body.get("tools"):
        chat["tools"] = convert_tools(body["tools"])
    chat["messages"] = ([{"role": "system", "content": instructions}] if instructions else []) + msgs
    chat["model"] = model
    return chat


def to_upstream(body: dict[str, Any], inbound: str, upstream: str, model: str) -> dict[str, Any]:
    """入站请求体 → 上游请求体。不支持的组合抛 `UnsupportedCombination`（前身口径：501）。"""
    if inbound == upstream or (inbound == INBOUND_CHAT and upstream == "chat"):
        out = dict(body)
        out["model"] = model
        return out
    if inbound == INBOUND_ANTHROPIC and upstream == "chat":
        return messages_to_chat_request(body, model)
    if inbound == INBOUND_CHAT and upstream == "anthropic":
        return chat_to_anthropic_request(body, model)
    if inbound == INBOUND_RESPONSES and upstream == "chat":
        return responses_to_chat_body(body, model)
    if inbound == INBOUND_RESPONSES and upstream == "anthropic":
        return responses_to_anthropic_request(body, model)
    raise UnsupportedCombination(f"不支持 {inbound} → {upstream} 的转换（前身同样不支持）")


# ----------------------------------------------------------------------
# 响应方向：上游 → 入站
# ----------------------------------------------------------------------


def _usage_map(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def chat_json_to_responses(up: dict[str, Any], model: str) -> dict[str, Any]:
    """Chat 非流式 JSON → Responses 非流式 JSON（前身 `_chat_json_to_responses` 原样移植）。"""
    choices = up.get("choices") or []
    text = ""
    tool_calls_out = []
    if choices:
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            tool_calls_out.append(
                {
                    "type": "function_call",
                    "id": call.get("id", ""),
                    "call_id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                }
            )
    output = []
    if text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    output.extend(tool_calls_out)
    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _usage_map(up.get("usage")),
        "parallel_tool_calls": True,
    }


def extract_upstream_text(up: dict[str, Any]) -> str:
    """从上游响应里取回复正文（chat／anthropic／responses 三形态都认）。"""
    choices = up.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content") or ""
    content = up.get("content")
    if isinstance(content, list) and content:
        return content[0].get("text", "") or ""
    output = up.get("output") or []
    if output and isinstance(output[0], dict):
        blocks = output[0].get("content") or []
        if blocks:
            return blocks[0].get("text", "") or ""
    return ""


def from_upstream(up: dict[str, Any], inbound: str, upstream: str, model: str) -> dict[str, Any]:
    """上游响应体 → 入站格式的响应体。"""
    if inbound == INBOUND_CHAT and upstream == "chat":
        return up
    if inbound == INBOUND_CHAT and upstream == "anthropic":
        return anthropic_to_chat_json(up, model)
    if inbound == INBOUND_ANTHROPIC and upstream == "anthropic":
        return up
    if inbound == INBOUND_ANTHROPIC and upstream == "chat":
        return chat_to_anthropic_json(up, model)
    if inbound == INBOUND_RESPONSES and upstream == "responses":
        return up
    if inbound == INBOUND_RESPONSES and upstream == "anthropic":
        return anthropic_to_responses_json(up, model)
    if inbound == INBOUND_RESPONSES and upstream == "chat":
        return chat_json_to_responses(up, model)
    raise UnsupportedCombination(f"不支持 {upstream} → {inbound} 的响应转换")


# ----------------------------------------------------------------------
# 流式（单 delta：我们没做上游逐行流式转发，见 docs/proxy.md 的缺口清单）
# ----------------------------------------------------------------------


def anthropic_sse(text: str, model: str) -> Iterator[str]:
    """Anthropic Messages 的 SSE 事件序列（前身 `_confirm_anthropic_sse` 的形状）。"""
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _sse_event("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}})
    if text:
        yield _sse_event(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        )
    yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _sse_event(
        "message_delta",
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 0}},
    )
    yield _sse_event("message_stop", {"type": "message_stop"})
    yield "data: [DONE]\n\n"


def chat_sse(text: str, model: str) -> Iterator[str]:
    """OpenAI Chat 的 SSE（单 chunk + [DONE]）。"""
    chunk_id = "chatcmpl-" + uuid.uuid4().hex[:12]
    head = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"
    tail = dict(head)
    tail["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def responses_sse(text: str, model: str, usage: dict[str, Any] | None = None) -> list[bytes]:
    """Responses 的 SSE（Codex 期望的八事件序列，由随迁的 responses_adapter 构造）。

    注意返回类型：`StreamingResponse` 要的是**可迭代的分块**；直接把 `bytes` 塞进去，
    Starlette 会当成可迭代对象**逐字节当 int** 迭代（实测报
    `AttributeError: 'int' object has no attribute 'encode'`），所以包成单元素列表。
    """
    from hippocampus.proxy.responses_adapter import build_sse_stream

    return [build_sse_stream(text, model=model, usage=usage)]


__all__ = [
    "INBOUND_ANTHROPIC",
    "INBOUND_CHAT",
    "INBOUND_RESPONSES",
    "UPSTREAM_MODES",
    "UnsupportedCombination",
    "anthropic_sse",
    "apply_injection",
    "chat_json_to_responses",
    "chat_sse",
    "convert_tools",
    "detect_inbound",
    "extract_upstream_text",
    "extract_user_text",
    "from_upstream",
    "request_text",
    "responses_sse",
    "responses_to_chat_body",
    "to_upstream",
]
