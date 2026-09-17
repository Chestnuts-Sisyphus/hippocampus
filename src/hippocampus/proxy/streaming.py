"""真·流式转发（A1）：把上游 SSE **逐行**转发给客户端（不再是一次性单 delta）。

两条链：
1. **归一化**：上游行流（三种上游格式的 SSE）→ 文本增量序列（格式感知解析）；
2. **按入站格式重发**：chat／anthropic／responses 各家客户端期望的事件序列，
   每个文本增量发一个 delta 事件（有逐字效果）。

流结束后：完成固化（`core.consolidate`，B 轨：模型输出永不进正式记忆），
确认块作为**末尾 delta** 追加（A39：确认块由记忆层生成、代理追加）。

本模块只吃"行流 + 格式标签"，不认识 HTTP；便于单测与复用。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from hippocampus.proxy import formats
from hippocampus.proxy.format_converters import _sse_event  # noqa: PLC2701 - 复用既有 SSE 封装


def _extract_delta(line: str, upstream_kind: str) -> str | None:
    """从上游的一行 SSE 里提取文本增量；非文本事件返回 None。

    `upstream_kind`：chat／anthropic／responses 三种上游格式的解析口径。
    """
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if upstream_kind == "chat":
        choices = obj.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        return text if isinstance(text, str) and text else None
    if upstream_kind == "anthropic":
        if obj.get("type") == "content_block_delta":
            d = obj.get("delta") or {}
            if d.get("type") == "text_delta":
                text = d.get("text")
                return text if isinstance(text, str) and text else None
        return None
    if upstream_kind == "responses":
        if obj.get("type") == "response.output_text.delta":
            text = obj.get("delta")
            return text if isinstance(text, str) and text else None
    return None


def _chat_chunk(model: str, delta_text: str, finish_reason: str | None = None) -> str:
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": finish_reason}],
    }
    if finish_reason:
        chunk["choices"][0]["delta"] = {}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _anthropic_envelope(model: str) -> list[str]:
    """Anthropic 事件序列的头部（message_start → content_block_start）。"""
    return [
        _sse_event(
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
        ),
        _sse_event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
    ]


def _anthropic_delta_event(text: str) -> str:
    return _sse_event(
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    )


def _anthropic_tail() -> list[str]:
    return [
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 0}},
        ),
        _sse_event("message_stop", {"type": "message_stop"}),
        "data: [DONE]\n\n",
    ]


async def stream_forward(
    upstream_lines: AsyncIterator[str],
    *,
    inbound: str,
    upstream_kind: str,
    model: str,
    core: Any,
    scope: Any,
    user_text: str,
    confirm_text: str,
) -> AsyncIterator[str]:
    """把上游行流转发为客户端格式的 SSE 分块序列；流末固化＋确认块末尾 delta。"""
    collected: list[str] = []

    # 头部（anthropic／responses 有固定开场事件；chat 直接发 delta）
    if inbound == formats.INBOUND_ANTHROPIC:
        for line in _anthropic_envelope(model):
            yield line
    elif inbound == formats.INBOUND_RESPONSES:
        resp_id = f"resp_{uuid.uuid4().hex[:24]}"
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        ts = int(time.time())
        yield _sse_event(
            "response.created",
            {"type": "response.created",
             "response": {"id": resp_id, "object": "response", "created_at": ts,
                          "status": "in_progress", "model": model, "output": []}},
        )
        yield _sse_event(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "message", "id": msg_id, "role": "assistant",
                      "status": "in_progress", "content": []}},
        )
        yield _sse_event(
            "response.content_part.added",
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": ""}},
        )

    # 逐行转发（每个文本增量一个 delta 事件）
    async for line in upstream_lines:
        text = _extract_delta(line, upstream_kind)
        if not text:
            continue
        collected.append(text)
        if inbound == formats.INBOUND_ANTHROPIC:
            yield _anthropic_delta_event(text)
        elif inbound == formats.INBOUND_RESPONSES:
            yield _sse_event(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
            )
        else:
            yield _chat_chunk(model, text)

    full_text = "".join(collected)

    # 流末固化：B 轨（模型输出永不进正式记忆）；取本轮确认块一起作末尾 delta
    turn_confirm = ""
    try:
        turn = core.consolidate(scope, user_text=user_text, assistant_text=full_text)
        turn_confirm = turn.confirm_block or ""
    except Exception as e:  # 软失败：固化坏掉不能影响已发出的流
        import sys

        sys.stderr.write(f"[proxy] 流末固化失败（不中断）: {e}\n")

    # 确认块作为末尾 delta 追加（A39：记忆层生成、代理追加；入口确认结果 + 本轮固化确认块）
    tail_text = (confirm_text + turn_confirm).strip()
    if tail_text:
        if inbound == formats.INBOUND_ANTHROPIC:
            yield _anthropic_delta_event(tail_text)
        elif inbound == formats.INBOUND_RESPONSES:
            yield _sse_event(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                 "delta": tail_text},
            )
        else:
            yield _chat_chunk(model, tail_text)

    # 收尾事件
    if inbound == formats.INBOUND_ANTHROPIC:
        for line in _anthropic_tail():
            yield line
    elif inbound == formats.INBOUND_RESPONSES:
        all_text = full_text + tail_text
        yield _sse_event(
            "response.output_text.done",
            {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": all_text},
        )
        yield _sse_event(
            "response.content_part.done",
            {"type": "response.content_part.done", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": all_text}},
        )
        yield _sse_event(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}", "role": "assistant",
                      "status": "completed", "content": [{"type": "output_text", "text": all_text}]}},
        )
        yield _sse_event(
            "response.completed",
            {"type": "response.completed",
             "response": {"id": f"resp_{uuid.uuid4().hex[:24]}", "object": "response",
                          "created_at": int(time.time()), "status": "completed", "model": model,
                          "output": [{"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}",
                                      "role": "assistant", "status": "completed",
                                      "content": [{"type": "output_text", "text": all_text}]}]}},
        )
        yield "data: [DONE]\n\n"
    else:
        yield _chat_chunk(model, "", finish_reason="stop")
        yield "data: [DONE]\n\n"


__all__ = ["_extract_delta", "stream_forward"]
