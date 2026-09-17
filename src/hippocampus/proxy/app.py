"""代理形态：**三种入站格式**的兼容端点（改 base_url 即获得记忆）。

一条请求的完整路径（**每一步的归属写清楚，便于面试时讲**）：

    客户端 → [1] 识别格式     （本代理：chat／responses／anthropic 按请求体形状）
           → [2] 解析 scope    （本代理：按头，非法即 400）
           → [3] 记忆注入      （记忆层：检索 → 过滤 → 分层拼接 → **按格式落位**）
           → [4] 转发上游      （按 api_mode 转到 chat／anthropic／responses 端点）
           → [5] 响应后固化    （记忆层：抽取 → 消歧 → 去重 → 冲突 → 挂起确认）
           → [6] 确认块追加    （**记忆层生成，本代理追加**；可关）
           → 客户端（**以它自己那一套格式**收到回复）

三条纪律：
1. **确认块不由模型生成**（A39）：记忆层按固定格式产出、代理拼在回复末尾；关掉开关就不追加；
   `确认 n`／`否决 n` 在**请求入口**被消费，不转发给上游。
2. **格式矩阵与前身一致**（见 `formats.py` 表）：chat→chat 透传、anthropic→chat 转换、
   responses→chat／anthropic 转换；不支持的组合回 501（前身同样不支持），不假装能转。
3. 危险动作（写文件/抓取）不在此形态出现——代理形态只做记忆，动作归 Agent 形态。

离线档（`--offline` / 无端点）：不做上游转发，返回一个明确的"离线回执"（仍按入站格式封装），
但记忆的检索、注入、固化、确认**照常走**（CI 就靠这个不依赖任何 key）。
"""

# 注意：本文件**不用** `from __future__ import annotations`。
# FastAPI 需要在运行时解析处理函数的注解来区分「路径/查询参数」与「请求对象」；
# 一旦注解被延迟成字符串，函数内局部导入的 `Request` 在模块全局里就找不到，
# FastAPI 会把 request 当成查询参数（实测得到 422 "Field required: query.request"）。
# 保持注解即时求值 + 函数内导入 fastapi，就能让 fastapi 仍是可选依赖。

import time
from pathlib import Path
from typing import Any

from hippocampus import settings as mem_config
from hippocampus.core import MemoryCore, Scope
from hippocampus.proxy import formats

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
    """构造 FastAPI 应用。

    `upstream(payload, *, model, endpoint, stream, body)` 为转发函数（None=离线档）：
    收**已转换好的上游请求体**，返回 `(上游响应 dict, usage)`。
    """
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
            "formats": [formats.INBOUND_CHAT, formats.INBOUND_RESPONSES, formats.INBOUND_ANTHROPIC],
            "upstream_endpoint": mem_config.upstream_endpoint(),
            "embedding": core.embedding_tier(),
            "stats": core.stats(scope),
            "lock": core.lock_status(scope),
        }

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "hippocampus", "object": "model", "owned_by": "local"}]}

    async def _handle(request: Request, inbound: str):
        """三种入站格式共用的一条链（识别 → 注入 → 转发 → 固化 → 追加确认块）。"""
        body = await request.json()
        headers = dict(request.headers)
        try:
            scope = _scope_from(headers)
        except Exception as e:  # scope 头是外部输入：非法直接 400，不落到文件系统上
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"scope 标识非法: {e}", "type": "invalid_scope"}},
            )
        want_confirm = confirm_block and headers.get(CONFIRM_HEADER, "").lower() not in ("0", "false", "off")
        model = str(body.get("model") or mem_config.endpoint_model())
        stream = bool(body.get("stream"))

        # [入口] 确认指令消费：`确认 n` / `否决 n` 由记忆层处理，不转发给上游
        user_text = formats.extract_user_text(body, inbound)
        confirmation = None
        if user_text:
            confirmation = core.confirm(scope, user_text)

        # [注入] 按入站格式落位；seen_text 用整轮请求（防"重复注入模型已见内容"）
        seen = formats.request_text(body, inbound)
        injection = core.inject_finalize(scope, user_text or seen[-500:], seen_text=seen)
        if injection.text:
            formats.apply_injection(body, injection.stable_text, injection.fluid_text)

        confirm_text = ""
        if confirmation is not None:
            confirm_text = "\n\n---\n> 记忆·确认结果：" + confirmation.text

        # [离线档] 不转发，但记忆纪律照常（CI 靠这条不依赖任何 key）
        if offline or upstream is None:
            turn = core.consolidate(scope, user_text=user_text)
            reply = _offline_reply(confirmation, injection, inbound)
            if want_confirm and turn.confirm_block:
                confirm_text += turn.confirm_block
            return _respond(reply + confirm_text, inbound, model, stream, StreamingResponse, JSONResponse)

        # [转发] 入站格式 → 上游格式（不支持的组合回 501，与前置口径一致）
        upstream_kind = mem_config.upstream_endpoint()
        try:
            payload = formats.to_upstream(body, inbound, upstream_kind, model)
        except formats.UnsupportedCombination as e:
            return JSONResponse(status_code=501, content={"error": {"message": str(e), "type": "unsupported_format"}})
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": {"message": str(e), "type": "invalid_request"}})

        try:
            up_body, usage = upstream(payload, model=model, endpoint=upstream_kind, stream=False, body=body)
        except Exception as e:  # 上游异常 → 明确报错，不吞
            return JSONResponse(
                status_code=502,
                content={"error": {"message": f"上游调用失败: {e}", "type": "upstream_error"}},
            )

        reply = formats.extract_upstream_text(up_body) if isinstance(up_body, dict) else str(up_body)
        try:
            converted = formats.from_upstream(up_body, inbound, upstream_kind, model)
        except formats.UnsupportedCombination as e:
            return JSONResponse(status_code=501, content={"error": {"message": str(e), "type": "unsupported_format"}})

        # [固化] 响应后提炼（B 轨：模型输出永不进正式记忆）
        turn = core.consolidate(scope, user_text=user_text, assistant_text=reply)
        if want_confirm and turn.confirm_block:
            confirm_text += turn.confirm_block

        # 附了确认块 → 正文变了，转换结果要跟着改；没变就直接用转换结果
        if confirm_text:
            return _respond(
                reply + confirm_text, inbound, model, stream, StreamingResponse, JSONResponse, usage=usage
            )
        return _respond(
            reply, inbound, model, stream, StreamingResponse, JSONResponse, usage=usage, converted=converted
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        """OpenAI Chat Completions 入站（绝大多数客户端）。"""
        return await _handle(request, formats.INBOUND_CHAT)

    @app.post("/v1/responses")
    async def responses(request: Request):
        """OpenAI Responses API 入站（Codex 一类客户端）。"""
        return await _handle(request, formats.INBOUND_RESPONSES)

    @app.post("/v1/messages")
    async def messages(request: Request):
        """Anthropic Messages 入站（Claude Code 一类客户端）。"""
        return await _handle(request, formats.INBOUND_ANTHROPIC)

    return app


def _offline_reply(confirmation: Any, injection: Any, inbound: str) -> str:
    """离线档回执：明确说"没有上游模型"，但把记忆层做的事如实报出来。"""
    lines = [f"（离线档：未配置模型端点，本条为本地回执；入站格式 {inbound}；记忆纪律照常执行）"]
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


def _respond(
    text: str,
    inbound: str,
    model: str,
    stream: bool,
    StreamingResponse,
    JSONResponse,
    usage: dict | None = None,
    converted: dict | None = None,
):
    """按**入站格式**回协议正确的响应（非流式给 JSON，流式给该家协议的 SSE 序列）。"""
    if inbound == formats.INBOUND_RESPONSES:
        if not stream:
            payload = dict(converted or {})
            payload.setdefault("model", model)
            return JSONResponse(payload)
        return StreamingResponse(formats.responses_sse(text, model, usage), media_type="text/event-stream")

    if inbound == formats.INBOUND_ANTHROPIC:
        if not stream:
            if converted is not None:
                return JSONResponse(converted)
            return JSONResponse(_anthropic_json(text, model, usage))
        return StreamingResponse(formats.anthropic_sse(text, model), media_type="text/event-stream")

    # chat
    if not stream:
        if converted is not None and "choices" in converted:
            return JSONResponse(converted)
        return JSONResponse(
            {
                "id": "chatcmpl-" + __import__("uuid").uuid4().hex[:12],
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage or {},
            }
        )
    return StreamingResponse(formats.chat_sse(text, model), media_type="text/event-stream")


def _anthropic_json(text: str, model: str, usage: dict | None = None) -> dict:
    """离线档也要按 Anthropic 的响应形状回（没有上游可转换时用）。"""
    import uuid as _uuid

    usage = usage or {}
    return {
        "id": "msg_" + _uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def make_upstream():
    """构造转发函数：把**已转换好的请求体**原样发到上游端点（chat／anthropic／responses）。

    凭据不经过本模块：走 `settings.llm_post_json`——那是记忆层的转发通道，
    **全项目唯一读凭据的地方**，端点 URL 的校验也在那里做。本层只负责转发与追加确认块。
    非 2xx 如实抛出（带状态码与上游原文），由入口转成 502 给客户端。
    """

    def _upstream(payload: dict, *, model: str, endpoint: str, stream: bool = False, body: dict | None = None):
        from hippocampus.settings import llm_post_json

        status, data = llm_post_json(payload, endpoint=endpoint)
        if status // 100 != 2:
            raise RuntimeError(f"上游返回 {status}: {str(data)[:300]}")
        return data, (data.get("usage") or {})

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
    print(f"Hippocampus 代理形态监听 http://{host}:{port}")
    print("  支持三种入站：/v1/chat/completions（OpenAI Chat）、/v1/responses（Responses）、"
          "/v1/messages（Anthropic Messages）")
    print(f"  上游端点按 config 的 llm.api_mode：当前 {mem_config.upstream_endpoint()}")
    print("  把客户端的 base_url 指到 http://host:port 即可；/health 可查格式、端口、锁、索引。")
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        core.close()
    return 0


__all__ = ["CONFIRM_HEADER", "build_app", "make_upstream", "serve"]
