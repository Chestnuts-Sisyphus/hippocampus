# 由前身 hippocampus_prototype/responses_adapter.py 抽取移植（vendoring），仅做包内 import 改写。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus API 代理 -- Responses API 适配器
负责 OpenAI Responses API <-> DeepSeek Chat Completions 格式互转。

请求方向（Responses -> DeepSeek）：
  - input 字符串 -> [{role:user, content: str}]
  - input 数组 -> 逐项解析为 messages（user/assistant/system）
  - instructions -> system message（合并到 messages 最前）
  - 记忆注入追加到 instructions

响应方向（DeepSeek -> Responses）：
  - DeepSeek {choices[0].message.content} -> Responses {output:[{type:message, content:[{type:output_text, text}]}]}
"""

import json
import time
import uuid


def _extract_text_from_content(content) -> str:
    """从 Responses API content 字段提取纯文本。
    content 可能是字符串，或 [{type: input_text/output_text, text: "..."}]"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def parse_request(body: dict) -> dict:
    """解析 Responses API 请求体，返回标准化的内部格式。
    返回: {model, instructions, messages, stream, tools, store, previous_response_id, raw}
    解析失败抛 ValueError（含中文消息，供 proxy_server 返回 400）。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")

    model = body.get("model", "deepseek-v4-flash")
    instructions = body.get("instructions", "") or ""
    stream = body.get("stream", False)
    tools = body.get("tools", [])
    store = body.get("store", True)
    prev_id = body.get("previous_response_id")

    inp = body.get("input")
    messages = []

    if inp is None:
        raise ValueError("input 字段缺失")
    if inp == "" or inp == []:
        raise ValueError("input 不能为空")

    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            content = item.get("content", "")
            text = _extract_text_from_content(content)
            if text:
                messages.append({"role": role, "content": text})
        if not messages:
            raise ValueError("input 数组中无有效消息")
    else:
        raise ValueError(f"input 类型不支持: {type(inp).__name__}")

    return {
        "model": model,
        "instructions": instructions,
        "messages": messages,
        "stream": stream,
        "tools": tools,
        "store": store,
        "previous_response_id": prev_id,
    }


def build_llm_messages(parsed: dict, injection: str = "") -> list[dict]:
    """把解析后的请求 + 记忆注入 -> DeepSeek messages 数组。
    instructions + injection 合并为 system message 放最前。
    role 映射：developer -> system（DeepSeek 不认 developer 角色）。
    """
    messages = []
    sys_parts = []
    if parsed["instructions"]:
        sys_parts.append(parsed["instructions"])
    if injection:
        sys_parts.append(injection)
    if sys_parts:
        messages.append({"role": "system", "content": "\n\n".join(sys_parts)})
    for msg in parsed["messages"]:
        role = msg["role"]
        # DeepSeek 只认 system/user/assistant，developer -> system
        if role == "developer":
            role = "system"
        messages.append({"role": role, "content": msg["content"]})
    return messages


def build_response(text: str, model: str = "deepseek-v4-flash", usage: dict | None = None) -> dict:
    """构造 Responses API 格式的响应体。"""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    usage = usage or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "parallel_tool_calls": True,
    }


def build_error_response(message: str, status_code: int = 400) -> tuple[dict, int]:
    """构造错误响应体 + HTTP 状态码。"""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": status_code,
        }
    }, status_code


def build_sse_stream(text: str, model: str = "deepseek-v4-flash", usage: dict | None = None) -> bytes:
    """构造 Responses API SSE 流式响应（伪流式：一次性发送完整内容）。
    Codex 期望的 SSE 事件序列：
      1. response.created      -- 响应对象创建
      2. response.output_item.added -- output 数组新增 message item
      3. response.content_part.added -- message item 新增 content part
      4. response.output_text.delta  -- 文本增量（可多次）
      5. response.output_text.done   -- 文本完成
      6. response.content_part.done  -- content part 完成
      7. response.output_item.done   -- output item 完成
      8. response.completed    -- 响应完成
    """
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    usage = usage or {}
    ts = int(time.time())

    events = []

    # 1. response.created
    events.append(
        (
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": ts,
                    "status": "in_progress",
                    "model": model,
                    "output": [],
                },
            },
        )
    )

    # 2. response.output_item.added
    events.append(
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": msg_id,
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )
    )

    # 3. response.content_part.added
    events.append(
        (
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            },
        )
    )

    # 4. response.output_text.delta (一次性发全部)
    events.append(
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            },
        )
    )

    # 5. response.output_text.done
    events.append(
        (
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
        )
    )

    # 6. response.content_part.done
    events.append(
        (
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text},
            },
        )
    )

    # 7. response.output_item.done
    events.append(
        (
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": msg_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                },
            },
        )
    )

    # 8. response.completed
    events.append(
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": resp_id,
                    "object": "response",
                    "created_at": ts,
                    "status": "completed",
                    "model": model,
                    "output": [
                        {
                            "type": "message",
                            "id": msg_id,
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ],
                    "usage": {
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    "parallel_tool_calls": True,
                },
            },
        )
    )

    # 编码为 SSE 格式
    lines = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
        lines.append("")  # 空行分隔
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def build_sse_error(message: str) -> bytes:
    """构造 SSE 格式的错误响应。"""
    data = {"type": "error", "error": {"message": message}}
    return f"event: error\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
