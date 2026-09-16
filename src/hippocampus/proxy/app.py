"""代理形态：OpenAI 兼容端点（任何兼容客户端改 base_url 即获得记忆）。

一条请求的完整路径（**每一步的归属写清楚，便于面试时讲**）：

    客户端 → [1] 解析 scope   （本代理，按令牌/头）
           → [2] 记忆注入     （记忆层：检索 → 过滤 → 分层拼接，前置到 messages）
           → [3] 转发上游     （模型端点，OpenAI 兼容）
           → [4] 响应后固化   （记忆层：抽取 → 消歧 → 去重 → 冲突 → 挂起确认）
           → [5] 确认块追加   （**记忆层生成，本代理追加**；可关）
           → 客户端

两条纪律：
1. **确认块不由模型生成**（A39）：它由记忆层按固定格式产出、由本代理拼在回复末尾；
   关掉开关就不追加；`确认 n`／`否决 n` 在**请求入口**被消费，不会转发给上游。
2. 危险动作（写文件/抓取）不在此形态出现——代理形态只做记忆，动作归 Agent 形态。

离线档（`--offline` / 无端点）：不做上游转发，返回一个明确的"离线回执"，
但记忆的检索、注入、固化、确认**照常走**（CI 就靠这个不依赖任何 key）。
"""

# 注意：本文件**不用** `from __future__ import annotations`。
# FastAPI 需要在运行时解析处理函数的注解来区分「路径/查询参数」与「请求对象」；
# 一旦注解被延迟成字符串，函数内局部导入的 `Request` 在模块全局里就找不到，
# FastAPI 会把 request 当成查询参数（实测得到 422 "Field required: query.request"）。
# 保持注解即时求值 + 函数内导入 fastapi，就能让 fastapi 仍是可选依赖。

import json
import time
import uuid
from pathlib import Path
from typing import Any

from hippocampus import settings as mem_config
from hippocampus.core import MemoryCore, Scope

CONFIRM_HEADER = "x-hippocampus-confirm"  # 客户端可带此头声明"这条回复不追加确认块"


def _scope_from(headers: dict[str, str], default_account: str = "default") -> Scope:
    """从请求头解析 scope。

    - account：`X-Hippocampus-Account`；缺省用 default（单机单用户是设计目标，
      不是"多租户"——头只是给"我想分开几个记忆库"的人留的口子）
    - session：`X-Hippocampus-Session` 或常见的 `X-Session-Id`；缺省按日期分桶，
      保证"同一段对话的上下文连续、跨天不串"

    头是**外部输入**：account 立即过目录穿越校验（非法在入口就拒，不落到文件系统上）。
    """
    from hippocampus.settings import validate_scope_id

    head = {k.lower(): v for k, v in headers.items()}
    account = validate_scope_id(head.get("x-hippocampus-account") or default_account)
    session = head.get("x-hippocampus-session") or head.get("x-session-id") or ""
    if not session:
        session = "day-" + time.strftime("%Y%m%d")
    return Scope(account=account, session=session[:64], source="user")


def build_app(core: MemoryCore, *, confirm_block: bool = True, offline: bool = False, upstream: Any = None):
    """构造 FastAPI 应用。`upstream(messages, model, stream)` 为转发函数（None=离线档）。"""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="Hippocampus", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        scope = Scope()
        return {
            "ok": True,
            "offline": offline,
            "confirm_block": confirm_block,
            "port": mem_config.proxy_config()["port"],
            "embedding": core.embedding_tier(),
            "stats": core.stats(scope),
            "lock": core.lock_status(scope),
        }

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "hippocampus", "object": "model", "owned_by": "local"}]}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        headers = dict(request.headers)
        try:
            scope = _scope_from(headers)
        except Exception as e:  # scope 头是外部输入：非法直接 400，不落到文件系统上
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"scope 标识非法: {e}", "type": "invalid_scope"}},
            )
        messages: list[dict[str, Any]] = body.get("messages") or []
        want_confirm = confirm_block and headers.get(CONFIRM_HEADER, "").lower() not in ("0", "false", "off")
        model = str(body.get("model") or mem_config.endpoint_model())
        stream = bool(body.get("stream"))

        # [入口] 确认指令消费：`确认 n` / `否决 n` 由记忆层处理，不转发给上游
        user_text = _last_user_text(messages)
        confirmation = None
        if user_text:
            confirmation = core.confirm(scope, user_text)

        # [注入] 用"模型本轮会看到的内容"做防重复（已见内容不再注入）
        seen = "\n".join(str(m.get("content") or "") for m in messages)
        injection = core.inject_finalize(scope, user_text or seen[-500:], seen_text=seen)
        if injection.text:
            messages = _prepend_memory(messages, injection.text)

        # [转发] 上游（离线档则不转发）
        confirm_text = ""
        if confirmation is not None:
            confirm_text = "\n\n---\n> 记忆·确认结果：" + confirmation.text
        if offline or upstream is None:
            # 离线档仍走**记忆固化**（句式规则抽取，不需要模型）：否则"离线能聊天但记不住"
            # 就成了半条链——场景 C 要求离线档的显式记住／句式抽取／冲突挂起都在。
            turn = core.consolidate(scope, user_text=user_text)
            reply = _offline_reply(confirmation, injection, body)
            if want_confirm and turn.confirm_block:
                confirm_text += turn.confirm_block
            return _respond(reply + confirm_text, model, stream, StreamingResponse, JSONResponse)

        try:
            reply, usage = upstream(messages, model=model, stream=False, body=body)
        except Exception as e:  # 上游异常 → 明确报错，不吞
            return JSONResponse(status_code=502, content={"error": {"message": f"上游调用失败: {e}", "type": "upstream_error"}})

        # [固化] 响应后提炼（B 轨：模型输出永不进正式记忆）
        turn = core.consolidate(scope, user_text=user_text, assistant_text=reply)
        if want_confirm and turn.confirm_block:
            confirm_text += turn.confirm_block

        return _respond(reply + confirm_text, model, stream, StreamingResponse, JSONResponse, usage=usage)

    return app


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if str(m.get("role")) == "user":
            content = m.get("content")
            if isinstance(content, list):  # 多模态：取文本段
                content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


def _prepend_memory(messages: list[dict[str, Any]], memory_text: str) -> list[dict[str, Any]]:
    """把记忆作为 system 消息前置。**不改客户端原来的消息**（返回新列表）。"""
    out = [dict(m) for m in messages]
    block = {"role": "system", "content": memory_text}
    if out and str(out[0].get("role")) == "system":
        out[0] = {"role": "system", "content": str(out[0].get("content") or "") + "\n\n" + memory_text}
    else:
        out.insert(0, block)
    return out


def _offline_reply(confirmation: Any, injection: Any, body: dict[str, Any]) -> str:
    """离线档回执：明确说"没有上游模型"，但把记忆层做的事如实报出来。"""
    lines = ["（离线档：未配置模型端点，本条为本地回执；记忆纪律照常执行）"]
    if confirmation is not None:
        lines.append(f"确认处理：{confirmation.text}")
    if injection.enabled and injection.items:
        lines.append("本轮注入的记忆：")
        lines.extend(f"- [{i.kind}] {i.content}" for i in injection.items)
    elif not injection.enabled:
        lines.append("注入开关：已关闭（memory off）")
    else:
        lines.append("本轮没有检索到相关记忆。")
    return "\n".join(lines)


def _respond(text: str, model: str, stream: bool, StreamingResponse, JSONResponse, usage: dict | None = None):
    created = int(time.time())
    if not stream:
        return JSONResponse(
            {
                "id": "chatcmpl-" + uuid.uuid4().hex[:12],
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage or {},
            }
        )

    def _sse():
        chunk = {
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        done = dict(chunk)
        done["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream")


def make_upstream():
    """构造转发函数（OpenAI 兼容 chat/completions）。

    凭据不经过本模块：直接复用记忆层的模型客户端（`settings.llm_chat_messages`）——
    那是**全项目唯一读凭据的地方**，端点 URL 的校验也在那里做。本层只负责转发与追加确认块。
    """

    def _upstream(messages: list[dict[str, Any]], *, model: str, stream: bool = False, body: dict[str, Any] | None = None):
        from hippocampus.settings import endpoint_ready, llm_chat_messages

        if not endpoint_ready():
            raise RuntimeError("未配置模型端点或凭据")
        data = llm_chat_messages(
            messages,
            max_tokens=int((body or {}).get("max_tokens") or 2000),
            temperature=float((body or {}).get("temperature", 0.7)),
        )
        return data["content"], data.get("usage", {})

    return _upstream


def serve(*, host: str, port: int, home: str | Path | None = None, confirm_block: bool = True, offline: bool = False) -> int:
    """起服务。离线档不需要任何凭据；在线档从环境变量／密钥服务取凭据。"""
    import uvicorn

    core = MemoryCore(home=home)
    use_offline = offline or mem_config.is_offline()
    upstream = None if use_offline else make_upstream()
    if not use_offline and not mem_config.endpoint_ready():
        print("（未提供凭据：本进程将按离线档运行——记忆纪律可用，上游转发不可用）")
        upstream = None
    app = build_app(core, confirm_block=confirm_block, offline=upstream is None, upstream=upstream)
    print(f"Hippocampus 代理形态监听 http://{host}:{port}/v1  （确认块={'开' if confirm_block else '关'}）")
    print("把客户端的 base_url 指到该地址即可；/health 可查端口、锁、索引。")
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        core.close()
    return 0


__all__ = ["CONFIRM_HEADER", "build_app", "make_upstream", "serve"]
