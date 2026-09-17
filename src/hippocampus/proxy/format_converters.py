# 由前身 hippocampus_prototype/format_converters.py 抽取移植（vendoring），仅做包内 import 改写。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 透明代理 -- format_converters.py
跨格式转换函数：chat <-> anthropic <-> responses。
3 组转换（每组=请求转换+非流式响应转换+流式响应转换）+ tool_choice/tools 辅助函数。

组 1: chat->anthropic（Agent 发 chat，上游 anthropic）
组 2: messages->chat（Agent 发 anthropic messages，上游 chat）
组 3: responses->anthropic（Agent 发 responses，上游 anthropic）

tool_call id 原样保留（不重新生成，防破坏 prompt cache）。
max_tokens 转 anthropic 时缺失设默认 4096（anthropic 必填字段）。
"""

import json
import sys
import time
import uuid

import httpx

from hippocampus.proxy.llm_proxy import UpstreamError

# ==================== 辅助：SSE 事件构造 ====================


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_error(msg: str) -> str:
    return _sse_event("error", {"type": "error", "error": {"message": msg}})


# ==================== 辅助：tool_choice 互转 ====================


def tool_choice_chat_to_anthropic(tc):
    """chat tool_choice -> anthropic tool_choice。
    auto -> {type:auto} / none -> None(不发) / required -> {type:any}
    / {type:function,function:{name:X}} -> {type:tool,name:X}
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc == "auto":
            return {"type": "auto"}
        if tc == "none":
            return None
        if tc == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(tc, dict):
        if tc.get("type") == "function":
            fn = tc.get("function") or {}
            return {"type": "tool", "name": fn.get("name", "")}
        t = tc.get("type", "auto")
        if t in ("auto", "any"):
            return {"type": t}
        return {"type": "auto"}
    return {"type": "auto"}


def tool_choice_anthropic_to_chat(tc):
    """anthropic tool_choice -> chat tool_choice。
    {type:auto} -> "auto" / {type:any} -> "required" / {type:tool,name:X} -> {type:function,function:{name:X}}
    """
    if tc is None:
        return "auto"
    if isinstance(tc, dict):
        t = tc.get("type", "auto")
        if t == "auto":
            return "auto"
        if t == "any":
            return "required"
        if t == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def tool_choice_responses_to_anthropic(tc):
    """responses tool_choice -> anthropic tool_choice。
    "auto"/"none"/"required" 同 chat；{type:function,name:X} -> {type:tool,name:X}
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc == "auto":
            return {"type": "auto"}
        if tc == "none":
            return None
        if tc == "required":
            return {"type": "any"}
        return {"type": "auto"}
    if isinstance(tc, dict):
        if tc.get("type") == "function":
            return {"type": "tool", "name": tc.get("name", "")}
        t = tc.get("type", "auto")
        if t in ("auto", "any"):
            return {"type": t}
        return {"type": "auto"}
    return {"type": "auto"}


# ==================== 辅助：tools 互转 ====================


def tools_chat_to_anthropic(tools):
    """chat tools [{type:function,function:{name,description,parameters}}]
    -> anthropic tools [{name,description,input_schema}]
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function") or {}
            out.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
    return out


def tools_anthropic_to_chat(tools):
    """anthropic tools [{name,description,input_schema}]
    -> chat tools [{type:function,function:{name,description,parameters}}]
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def tools_responses_to_anthropic(tools):
    """responses tools [{type:function,name,description,parameters}]
    -> anthropic tools [{name,description,input_schema}]
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" or "name" in t:
            out.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
                }
            )
    return out


# ==================== 辅助：content 提取 ====================


def _extract_text(content) -> str:
    """content (str 或 [{type:text,text:...}]) -> 纯文本。"""
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


# ==================== cache_control 透传开关（任务书 3B） ====================

_CACHE_CONTROL_PASSTHROUGH = True


def set_cache_control_passthrough(value: bool) -> None:
    """设置 cache_control 透传开关。
    True=保留不删不改；False=剥全部 cache_control 字段（回退旧行为）。"""
    global _CACHE_CONTROL_PASSTHROUGH
    _CACHE_CONTROL_PASSTHROUGH = bool(value)


def _to_anthropic_text_blocks(content, passthrough: bool) -> list:
    """content (str 或 block list) -> anthropic text block 数组。
    passthrough=True 时保留 text/input_text 块的 cache_control 字段。
    非 text 块仅提取 text（行为同 _extract_text，不改）。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks = []
        for item in content:
            if isinstance(item, dict):
                itype = item.get("type", "")
                if itype in ("text", "input_text"):
                    blk = {"type": "text", "text": item.get("text", "")}
                    if passthrough and "cache_control" in item:
                        blk["cache_control"] = item["cache_control"]
                    blocks.append(blk)
            elif isinstance(item, str):
                blocks.append({"type": "text", "text": item})
        return blocks
    return []


# ==================== 组 1: chat -> anthropic ====================


def chat_to_anthropic_request(body: dict, model: str) -> dict:
    """chat 请求 -> anthropic 请求。
    system/developer 消息收集为 system 块数组（不再拍平成字符串）；
    assistant 有 tool_calls -> content 追加 {type:tool_use,id,name,input}；
    tool role -> {type:tool_result,tool_use_id,content}；
    tools: function->{name,description,input_schema}；
    tool_choice: 同 tool_choice_chat_to_anthropic；
    max_tokens 缺失->4096；model 覆盖。
    cache_control 透传：text 块的 cache_control 原样保留（开关=True）。
    """
    passthrough = _CACHE_CONTROL_PASSTHROUGH
    system_blocks = []  # 块数组（任务书 3B：不再拍平）
    msgs = []
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content")
        if role in ("system", "developer"):
            # content 为块数组 -> 块结构原样并入；content 为 str -> 转 text 块
            blocks = _to_anthropic_text_blocks(content, passthrough)
            system_blocks.extend(blocks)
            continue
        if role == "tool":
            call_id = m.get("tool_call_id", "")
            # content 为 str -> 原样；content 为块数组 -> 逐子块转 text 块
            # （text/input_text 子块保留 cache_control，受开关控制；任务书 3B 漏项修复）
            if isinstance(content, str):
                tool_content = content
            elif isinstance(content, list):
                tool_content = _to_anthropic_text_blocks(content, passthrough)
            else:
                tool_content = _extract_text(content) if content else ""
            msgs.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call_id, "content": tool_content}],
                }
            )
            continue
        if role == "assistant":
            blocks = []
            if content:
                if isinstance(content, str):
                    if content:
                        blocks.append({"type": "text", "text": content})
                else:
                    for blk_item in content if isinstance(content, list) else []:
                        if isinstance(blk_item, dict):
                            btype = blk_item.get("type", "")
                            if btype in ("text", "input_text"):
                                blk = {"type": "text", "text": blk_item.get("text", "")}
                                if passthrough and "cache_control" in blk_item:
                                    blk["cache_control"] = blk_item["cache_control"]
                                blocks.append(blk)
                            elif btype == "tool_use":
                                blocks.append(dict(blk_item))
                            else:
                                # 其他类型保留 text
                                txt = blk_item.get("text", "")
                                if txt:
                                    blocks.append({"type": "text", "text": txt})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args_str = fn.get("arguments", "{}")
                try:
                    args_obj = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    args_obj = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args_obj,
                    }
                )
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            msgs.append({"role": "assistant", "content": blocks})
            continue
        # user
        blocks = _to_anthropic_text_blocks(content, passthrough)
        msgs.append({"role": "user", "content": blocks})

    out = {"model": model, "messages": msgs}
    if system_blocks:
        out["system"] = system_blocks
    if body.get("tools"):
        out["tools"] = tools_chat_to_anthropic(body["tools"])
    tc = body.get("tool_choice")
    if tc is not None:
        converted = tool_choice_chat_to_anthropic(tc)
        if converted is not None:
            out["tool_choice"] = converted
    # max_tokens: anthropic 必填
    out["max_tokens"] = body.get("max_tokens", 4096)
    # 透传其他参数
    for k, v in body.items():
        if k in ("model", "messages", "tools", "tool_choice", "max_tokens", "n"):
            continue
        out[k] = v
    return out


def anthropic_to_chat_json(up: dict, model: str) -> dict:
    """anthropic 非流式 JSON -> chat 非流式 JSON。
    content type=text 拼成 content 字符串；type=tool_use 转 tool_calls。
    """
    content_blocks = up.get("content") or []
    text_parts = []
    tool_calls = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    up.get("stop_reason", "end_turn")
    finish_reason = "tool_calls" if tool_calls else "stop"
    return {
        "id": up.get("id", f"chatcmpl_{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": (up.get("usage") or {}).get("input_tokens", 0),
            "completion_tokens": (up.get("usage") or {}).get("output_tokens", 0),
            "total_tokens": (up.get("usage") or {}).get("input_tokens", 0)
            + (up.get("usage") or {}).get("output_tokens", 0),
        },
    }


async def anthropic_to_chat_sse(upstream_gen, model: str, after_task=None, on_complete=None):
    """anthropic SSE -> chat SSE。
    message_start 不发；content_block_delta(text_delta)->chat delta.content；
    content_block_start(tool_use)->chat delta.tool_calls(初始)；
    content_block_delta(input_json_delta)->chat delta.tool_calls.arguments；
    message_stop->[DONE]。
    after_task: [DONE] 前 await，追加 delta.content=确认文本。
    on_complete: 转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）。
    """
    f"chatcmpl_{uuid.uuid4().hex[:24]}"
    text_parts = []
    tool_states = {}  # index -> {id, name, args}
    tool_emitted = set()
    first_chunk = True

    try:
        async for raw in upstream_gen:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue

            etype = obj.get("type", "")

            if etype == "content_block_start":
                block = obj.get("content_block") or {}
                idx = obj.get("index", 0)
                if block.get("type") == "tool_use":
                    st = tool_states.setdefault(
                        idx, {"id": block.get("id", ""), "name": block.get("name", ""), "args": []}
                    )
                    chunk = {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": idx,
                                            "id": st["id"],
                                            "type": "function",
                                            "function": {"name": st["name"], "arguments": ""},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                    if first_chunk:
                        chunk["choices"][0]["delta"]["role"] = "assistant"
                        first_chunk = False
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    tool_emitted.add(idx)

            elif etype == "content_block_delta":
                idx = obj.get("index", 0)
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        chunk = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                        if first_chunk:
                            chunk["choices"][0]["delta"]["role"] = "assistant"
                            first_chunk = False
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        text_parts.append(text)
                elif delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if partial and idx in tool_states:
                        tool_states[idx]["args"].append(partial)
                        chunk = {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": idx,
                                                "function": {"arguments": partial},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ]
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            elif etype == "message_delta":
                pass  # stop_reason 处理在 message_stop

            elif etype == "message_stop":
                pass  # 收尾处理

    except UpstreamError:
        yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return
    except httpx.HTTPError:
        yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    # 轨道B：转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）
    if on_complete is not None:
        try:
            full_text = "".join(text_parts)
            if full_text:
                on_complete(full_text)
        except Exception as e:
            sys.stderr.write(f"[proxy] anthropic->chat 转换轨道B回调失败（软失败）: {e}\n")

    # 记忆提炼确认块
    if after_task is not None:
        try:
            extra = await after_task
        except Exception:
            extra = ""
        if extra:
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': extra}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            text_parts.append(extra)

    # 收尾 chunk
    yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ==================== 组 2: messages -> chat ====================


def messages_to_chat_request(body: dict, model: str) -> dict:
    """anthropic messages 请求 -> chat 请求。
    system->messages 开头 {role:system}；
    content list 提取 text 拼 str；assistant tool_use->{role:assistant,content:null,tool_calls}；
    user tool_result->{role:tool,tool_call_id,content}；
    tools: {name,description,input_schema}->{type:function,function:{name,description,parameters}}；
    tool_choice: 同 tool_choice_anthropic_to_chat；model 覆盖。
    """
    system = body.get("system", "")
    msgs = []
    if system:
        if isinstance(system, list):
            # anthropic->chat 方向：chat 端无 cache_control 概念，
            # 拍平为字符串时丢弃 cache_control 属必然（拍板 1 注释说明，不改行为）
            sys_text = _extract_text(system)
        else:
            sys_text = system
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})

    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            msgs.append({"role": role, "content": ""})
            continue

        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                            },
                        }
                    )
            msg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
        elif role == "user":
            # user 可能含 tool_result
            has_tool_result = False
            text_parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    has_tool_result = True
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        tool_content = _extract_text(tool_content)
                    msgs.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tool_content if isinstance(tool_content, str) else str(tool_content),
                        }
                    )
                elif block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                msgs.append({"role": "user", "content": "".join(text_parts)})
            elif not has_tool_result:
                msgs.append({"role": "user", "content": ""})
        else:
            text = _extract_text(content)
            msgs.append({"role": role, "content": text})

    out = {"model": model, "messages": msgs}
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    if body.get("tools"):
        out["tools"] = tools_anthropic_to_chat(body["tools"])
    tc = body.get("tool_choice")
    if tc is not None:
        out["tool_choice"] = tool_choice_anthropic_to_chat(tc)
    # 透传其他参数
    for k, v in body.items():
        if k in ("model", "messages", "system", "tools", "tool_choice", "max_tokens"):
            continue
        out[k] = v
    return out


def chat_to_anthropic_json(up: dict, model: str) -> dict:
    """chat 非流式 JSON -> anthropic 非流式 JSON。
    choices[0].message.content->content:[{type:text,text}]；
    tool_calls->content 追加 {type:tool_use,id,name,input}。
    """
    choices = up.get("choices") or []
    content_blocks = []
    if choices:
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if text:
            content_blocks.append({"type": "text", "text": text})
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_str = fn.get("arguments", "{}")
            try:
                args_obj = json.loads(args_str) if args_str else {}
            except (json.JSONDecodeError, ValueError):
                args_obj = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args_obj,
                }
            )
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]
    finish = choices[0].get("finish_reason", "stop") if choices else "stop"
    stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": (up.get("usage") or {}).get("prompt_tokens", 0),
            "output_tokens": (up.get("usage") or {}).get("completion_tokens", 0),
        },
    }


async def chat_to_anthropic_sse(upstream_gen, model: str, after_task=None, on_complete=None):
    """chat SSE -> anthropic SSE。
    先发 message_start+content_block_start(text)；
    delta.content->content_block_delta(text_delta)；
    delta.tool_calls 首次->content_block_start(tool_use)；
    delta.tool_calls.arguments->content_block_delta(input_json_delta)；
    [DONE]前->content_block_stop+message_delta+message_stop，再发[DONE]。
    after_task: [DONE] 前 await，追加 content_block_delta(text_delta)=确认文本。
    on_complete: 转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）。
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    int(time.time())

    # message_start
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
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

    text_block_started = False
    tool_states = {}  # index -> {id, name, args, block_started}
    block_indices = {}  # tool index -> content block index
    next_block_idx = 1  # 0 is text block
    text_parts = []
    usage = {}

    try:
        async for raw in upstream_gen:
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue

            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]

            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            content = delta.get("content")
            if content:
                if not text_block_started:
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    text_block_started = True
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content},
                    },
                )
                text_parts.append(content)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                st = tool_states.setdefault(idx, {"id": "", "name": "", "args": [], "block_started": False})
                if tc.get("id"):
                    st["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    st["name"] = fn["name"]

                if not st["block_started"]:
                    block_idx = next_block_idx
                    block_indices[idx] = block_idx
                    next_block_idx += 1
                    st["block_started"] = True
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_idx,
                            "content_block": {"type": "tool_use", "id": st["id"], "name": st["name"], "input": {}},
                        },
                    )

                if fn.get("arguments"):
                    st["args"].append(fn["arguments"])
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_indices[idx],
                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                        },
                    )

    except UpstreamError as e:
        yield _sse_error(f"上游错误 {e.status_code}: {e.body_text[:200]}")
        return
    except httpx.HTTPError as e:
        yield _sse_error(f"上游连接失败: {e}")
        return

    # 轨道B：转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）
    if on_complete is not None:
        try:
            full_text = "".join(text_parts)
            if full_text:
                on_complete(full_text)
        except Exception as e:
            sys.stderr.write(f"[proxy] chat->anthropic 转换轨道B回调失败（软失败）: {e}\n")

    # 记忆提炼确认块
    if after_task is not None:
        try:
            extra = await after_task
        except Exception:
            extra = ""
        if extra:
            if not text_block_started:
                yield _sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                text_block_started = True
            yield _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": extra},
                },
            )
            text_parts.append(extra)

    # 收尾：close text block
    if text_block_started:
        yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})

    # close tool blocks
    for idx, st in tool_states.items():
        if st["block_started"]:
            yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": block_indices[idx]})

    has_tools = bool(tool_states)
    stop_reason = "tool_use" if has_tools else "end_turn"

    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": (usage.get("completion_tokens", 0) if usage else 0)},
        },
    )
    yield _sse_event("message_stop", {"type": "message_stop"})
    yield "data: [DONE]\n\n"


# ==================== 组 3: responses -> anthropic ====================


def responses_to_anthropic_request(body: dict, model: str) -> dict:
    """responses 请求 -> anthropic 请求。
    instructions->system 块；input: str->[{role:user}]；
    input: list 逐项解析（同 _responses_to_chat_body 的 input 解析）；
    tools/tool_choice 同 chat->anthropic；max_tokens 缺失->4096；model 覆盖。
    cache_control 透传：input_text 块的 cache_control 原样保留（开关=True）。
    """
    passthrough = _CACHE_CONTROL_PASSTHROUGH
    system_blocks = []  # 块数组（任务书 3B：不再拍平）
    instructions = body.get("instructions") or ""
    if instructions:
        system_blocks.append({"type": "text", "text": instructions})

    inp = body.get("input")
    if inp is None:
        raise ValueError("input 字段缺失")

    msgs = []
    if isinstance(inp, str):
        if inp.strip():
            msgs.append({"role": "user", "content": [{"type": "text", "text": inp}]})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            itype = item.get("type", "message")
            if itype == "message":
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                content = item.get("content", "")
                if role == "system":
                    # system content 转块并入 system_blocks
                    blocks = _to_anthropic_text_blocks(content, passthrough)
                    system_blocks.extend(blocks)
                else:
                    # user/assistant content 转块（透传 cache_control）
                    blocks = _to_anthropic_text_blocks(content, passthrough)
                    if blocks:
                        msgs.append({"role": role, "content": blocks})
            elif itype == "function_call":
                call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}"
                arguments = item.get("arguments") or "{}"
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                try:
                    args_obj = json.loads(arguments) if arguments else {}
                except (json.JSONDecodeError, ValueError):
                    args_obj = {}
                msgs.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": item.get("name", ""),
                                "input": args_obj,
                            }
                        ],
                    }
                )
            elif itype == "function_call_output":
                call_id = item.get("call_id") or f"call_{uuid.uuid4().hex[:16]}"
                output = item.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output)
                msgs.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output}],
                    }
                )
    else:
        raise ValueError(f"input 类型不支持: {type(inp).__name__}")

    if not msgs:
        raise ValueError("input 中无有效消息")

    out = {"model": model, "messages": msgs}
    if system_blocks:
        out["system"] = system_blocks
    if body.get("tools"):
        out["tools"] = tools_responses_to_anthropic(body["tools"])
    tc = body.get("tool_choice")
    if tc is not None:
        converted = tool_choice_responses_to_anthropic(tc)
        if converted is not None:
            out["tool_choice"] = converted
    out["max_tokens"] = body.get("max_tokens", 4096)
    # 透传其他参数
    for k, v in body.items():
        if k in ("model", "input", "instructions", "tools", "tool_choice", "max_tokens"):
            continue
        out[k] = v
    return out


def anthropic_to_responses_json(up: dict, model: str) -> dict:
    """anthropic 非流式 JSON -> responses 非流式 JSON。
    content type=text->output message；type=tool_use->output function_call。
    """
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    ts = int(time.time())
    content_blocks = up.get("content") or []
    output = []
    text_parts = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:16]}",
                    "call_id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    "status": "completed",
                }
            )
    if text_parts:
        output.insert(
            0,
            {
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "".join(text_parts)}],
            },
        )
    usage = up.get("usage") or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": ts,
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
        "parallel_tool_calls": True,
    }


async def anthropic_to_responses_sse(upstream_gen, model: str, after_task=None, on_complete=None):
    """anthropic SSE -> responses SSE。
    发 response.created+output_item.added+content_part.added；
    text_delta->response.output_text.delta；
    tool_use->output_item.added(function_call)；
    input_json_delta->function_call_arguments.delta；
    message_stop->收尾事件(output_text.done/content_part.done/output_item.done/response.completed)+[DONE]。
    after_task: [DONE] 前 await，追加 response.output_text.delta=确认文本。
    on_complete: 转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）。
    """
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    ts = int(time.time())

    yield _sse_event(
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

    msg_started = False
    text_parts = []
    tool_states = {}  # anthropic block index -> {id, name, args, output_index, emitted}
    tool_output_index = 1  # 0 is message
    usage = {}

    try:
        async for raw in upstream_gen:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue

            etype = obj.get("type", "")

            if etype == "message_start":
                msg_obj = obj.get("message") or {}
                if isinstance(msg_obj.get("usage"), dict):
                    u = msg_obj["usage"]
                    usage["input_tokens"] = u.get("input_tokens", 0)

            elif etype == "content_block_start":
                block = obj.get("content_block") or {}
                idx = obj.get("index", 0)
                if block.get("type") == "tool_use":
                    st = tool_states.setdefault(
                        idx,
                        {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "args": [],
                            "output_index": tool_output_index,
                            "emitted": True,
                        },
                    )
                    yield _sse_event(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": tool_output_index,
                            "item": {
                                "type": "function_call",
                                "id": f"fc_{uuid.uuid4().hex[:16]}",
                                "call_id": st["id"],
                                "name": st["name"],
                                "arguments": "",
                                "status": "in_progress",
                            },
                        },
                    )
                    tool_output_index += 1

            elif etype == "content_block_delta":
                idx = obj.get("index", 0)
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        if not msg_started:
                            yield _sse_event(
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
                            yield _sse_event(
                                "response.content_part.added",
                                {
                                    "type": "response.content_part.added",
                                    "output_index": 0,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                },
                            )
                            msg_started = True
                        yield _sse_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": text,
                            },
                        )
                        text_parts.append(text)
                elif delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if partial and idx in tool_states:
                        st = tool_states[idx]
                        st["args"].append(partial)
                        yield _sse_event(
                            "function_call_arguments.delta",
                            {
                                "type": "function_call_arguments.delta",
                                "output_index": st["output_index"],
                                "item_id": st["id"],
                                "delta": partial,
                            },
                        )

            elif etype == "message_delta":
                u = obj.get("usage") or {}
                if u:
                    usage["output_tokens"] = u.get("output_tokens", 0)

    except UpstreamError as e:
        yield _sse_error(f"上游错误 {e.status_code}: {e.body_text[:200]}")
        return
    except httpx.HTTPError as e:
        yield _sse_error(f"上游连接失败: {e}")
        return

    # 轨道B：转换结束、确认块拼接前，回调累积全文（排除确认块 extra；软失败）
    if on_complete is not None:
        try:
            full_text = "".join(text_parts)
            if full_text:
                on_complete(full_text)
        except Exception as e:
            sys.stderr.write(f"[proxy] anthropic->responses 转换轨道B回调失败（软失败）: {e}\n")

    # 记忆提炼确认块
    if after_task is not None:
        try:
            extra = await after_task
        except Exception:
            extra = ""
        if extra:
            if not msg_started:
                yield _sse_event(
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
                yield _sse_event(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
                msg_started = True
            yield _sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": extra,
                },
            )
            text_parts.append(extra)

    # 收尾事件
    text = "".join(text_parts)
    if msg_started:
        yield _sse_event(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
        )
        yield _sse_event(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": text},
            },
        )
        yield _sse_event(
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

    # 工具收尾
    out_items = []
    if msg_started:
        out_items.append(
            {
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for _idx, st in tool_states.items():
        full_args = "".join(st["args"])
        yield _sse_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": st["output_index"],
                "item": {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:16]}",
                    "call_id": st["id"],
                    "name": st["name"],
                    "arguments": full_args,
                    "status": "completed",
                },
            },
        )
        out_items.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:16]}",
                "call_id": st["id"],
                "name": st["name"],
                "arguments": full_args,
            }
        )

    yield _sse_event(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": resp_id,
                "object": "response",
                "created_at": ts,
                "status": "completed",
                "model": model,
                "output": out_items,
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
                "parallel_tool_calls": True,
            },
        },
    )
    yield "data: [DONE]\n\n"
