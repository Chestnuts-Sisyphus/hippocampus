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

import sys
import time
from pathlib import Path
from typing import Any

from hippocampus import settings as mem_config
from hippocampus.core import MemoryCore, Scope
from hippocampus.proxy import formats

CONFIRM_HEADER = "x-hippocampus-confirm"  # 客户端可带此头声明"这条回复不追加确认块"


class UpstreamHTTPError(RuntimeError):
    """上游非 2xx（A3）：带状态码与上游错误体，入口**原样透传**给客户端。"""

    def __init__(self, status_code: int, body: Any):
        super().__init__(f"上游返回 {status_code}: {str(body)[:300]}")
        self.status_code = int(status_code)
        self.body = body


def _scope_from(headers: dict[str, str], default_account: str = "default", bucketing: str = "day") -> Scope:
    """从请求头解析 scope。

    - account：`X-Hippocampus-Account`；缺省用 default（单机单用户是设计目标，
      不是"多租户"——头只是给"我想分开几个记忆库"的人留的口子）
    - session：`X-Hippocampus-Session` 或常见的 `X-Session-Id`；**显式传了就按传的**。
      没传时按配置 `proxy.session_bucketing` 分桶（A7 可配）：
        - `day`（默认）：`day-YYYYMMDD`——同一段对话的上下文连续、跨天不串；
        - `hour`：`hour-YYYYMMDDHH`——换得更勤，跨天续接的语义按小时切；
        - `none`：不按时间分桶（固定 `default`）——跨天续接同一个会话（由客户端自己管边界）。

    头是**外部输入**：account 立即过目录穿越校验（非法在入口就拒，不落到文件系统上）。
    """
    from hippocampus.settings import validate_scope_id

    head = {k.lower(): v for k, v in headers.items()}
    account = validate_scope_id(head.get("x-hippocampus-account") or default_account)
    session = head.get("x-hippocampus-session") or head.get("x-session-id") or ""
    if not session:
        bucketing = (bucketing or "day").lower()
        if bucketing == "hour":
            session = "hour-" + time.strftime("%Y%m%d%H")
        elif bucketing == "none":
            session = "default"
        else:
            session = "day-" + time.strftime("%Y%m%d")
    return Scope(account=account, session=session[:64], source="user")


def _unauthorized_body() -> dict[str, Any]:
    """缺/错实例令牌时的统一响应体（`/v1/*`、`/run`、`/trace` 共用一条鉴权口径）。"""
    return {
        "error": {
            "message": "未提供有效的实例令牌（首次启动生成的令牌见 `hippocampus doctor`）",
            "type": "unauthorized",
        }
    }


def build_app(
    core: MemoryCore,
    *,
    confirm_block: bool = True,
    offline: bool = False,
    upstream: Any = None,
    auth_token: str | None = None,
    health_requires_auth: bool = False,
    models_requires_auth: bool = False,
):
    """构造 FastAPI 应用。

    `upstream(payload, *, model, endpoint, stream, body)` 为转发函数（None=离线档）：
    收**已转换好的上游请求体**，返回 `(上游响应 dict, usage)`（非流式）或
    逐行 async 生成器（流式，`stream=True` 时）。

    `auth_token`：实例令牌（A2）。None＝从存储读取（`instance_token` 文件）；
    空串＝不鉴权（测试与无令牌环境）；非空＝要求 `Authorization: Bearer <token>`。

    `health_requires_auth`：八轮 V8。`/health` 自 0.1.0 起免鉴权（进程活着＋索引健康），
    环回档维持原样；服务形态绑到**非环回**时必须置 True——回体里的 stats／索引／锁是实质信息面。

    `models_requires_auth`：九轮 W1，与上一条同口径。`/v1/models` 回的是**配置的模型名**
    （本机装了哪个上游），两形态绑到非环回时同样必须带令牌；环回档免鉴权不变。
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    if auth_token is None:
        auth_token = mem_config.instance_token()

    def _authorized(request: Request) -> bool:
        if not auth_token:
            return True
        header = request.headers.get("authorization", "")
        return header == f"Bearer {auth_token}" or header == auth_token

    from hippocampus import __version__ as _pkg_version

    # 九轮 W2：版本单源——OpenAPI 文档里的版本号跟着包元数据走，不再另写死
    app = FastAPI(title="Hippocampus", version=_pkg_version, docs_url=None, redoc_url=None)

    class BodyTooLargeError(ValueError):
        """十轮 X3：请求体体积超过 `proxy.max_body_bytes`（由入口转 413）。"""

        def __init__(self, size: int, limit: int) -> None:
            super().__init__(f"请求体 {size} 字节超过上限 {limit} 字节")
            self.size = size
            self.limit = limit

    async def read_json_body(request: Request, limit: int) -> Any:
        """带体积上限地读 JSON 请求体。

        先看 `content-length`（省掉注定超限的传输），再按流累计**真实**字节数——头里的长度可以谎报，
        只信头等于没闸。`limit <= 0` ＝运维显式声明"不承诺上限"（口径见 `docs/proxy.md` §三·五）。
        """
        declared = str(request.headers.get("content-length") or "").strip()
        if limit > 0 and declared.isdigit() and int(declared) > limit:
            raise BodyTooLargeError(int(declared), limit)
        if limit <= 0:
            return await request.json()
        import json

        size = 0
        chunks: list[bytes] = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise BodyTooLargeError(size, limit)
            chunks.append(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))

    def _injected_view(injection: Any) -> list[dict]:
        return [
            {"id": i.id, "kind": i.kind, "content": i.content, "score": round(i.score, 4)}
            for i in (getattr(injection, "items", None) or [])
        ]

    def _dropped_view(injection: Any) -> list[dict]:
        return [{"id": d.id, "reason": d.reason, "stage": d.stage} for d in (getattr(injection, "dropped", None) or [])]

    def _consolidate_502(
        scope: Scope, injection: Any, exc: Exception, *, reply: str = "", form_hint: str = ""
    ) -> JSONResponse:
        """固化阶段失败的统一出口（十轮 X1）。

        模型口此前让 `core.consolidate` 抛出的异常穿透成 ASGI **裸 500**（既无 `stage` 也丢掉已完成
        的注入段），与管理口七轮定的"502＋回带已完成注入段"口径不一致。两形态现在共用这一个出口，
        判据＝遇"抽取器拿到非 JSON"必须同一状态码、同一形状。
        """
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": (
                        f"注入已完成、固化未完成：抽取阶段的模型端点调用失败（{type(exc).__name__}: {exc}）。"
                        "上游回体不是合法 JSON 时抽取器不猜格式，也不静默降级到规则档；"
                        "配不到可用端点时才走规则档。" + form_hint
                    ),
                    "stage": "consolidate",
                    "type": "consolidation_failed",
                },
                "run_id": getattr(injection, "run_id", ""),
                "scope": {"account": scope.account, "session": scope.session},
                "injected": _injected_view(injection),
                "dropped": _dropped_view(injection),
                "note": getattr(injection, "note", "") or "",
                "upstream_reply": reply,
            },
        )

    @app.get("/health")
    def health(request: Request):
        if health_requires_auth and not _authorized(request):
            return JSONResponse(status_code=401, content=_unauthorized_body())
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
            "index": core.index_health(scope),
            "lock": core.lock_status(scope),
        }

    @app.get("/v1/models")
    def models(request: Request) -> dict[str, Any]:
        """回**配置的**模型名（部分客户端会校验列表；占位串会让它们拒用）。

        顺序：`llm.model`（或 `HIPPOCAMPUS_MODEL`／`OPENAI_MODEL`）→ 内置默认名。
        注意列表里回的是"本代理接受并转发的模型名"，上游真实模型仍由 `llm.api_mode` 决定。

        鉴权（九轮 W1）：`models_requires_auth` 为真（= 绑到非环回）时与 `/health` 同口径要令牌。
        """
        if models_requires_auth and not _authorized(request):
            return JSONResponse(status_code=401, content=_unauthorized_body())
        name = mem_config.endpoint_model() or "hippocampus"
        return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local"}]}

    async def _handle(request: Request, inbound: str):
        """三种入站格式共用的一条链（识别 → 鉴权 → 注入 → 转发 → 固化 → 追加确认块）。"""
        if not _authorized(request):
            return JSONResponse(status_code=401, content=_unauthorized_body())
        max_body = int(mem_config.proxy_config().get("max_body_bytes") or 0)
        try:
            body = await read_json_body(request, max_body)
        except BodyTooLargeError as e:
            # 十轮 X3：体积上限（口径见 docs/proxy.md §三·五）
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": str(e),
                        "type": "body_too_large",
                        "max_body_bytes": e.limit,
                    }
                },
            )
        except Exception as e:
            # 请求体不是合法 JSON（含编码问题）：回 400，不要把解析错误包成 500
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"请求体不是合法 JSON（UTF-8）: {e}", "type": "invalid_request"}},
            )
        headers = dict(request.headers)
        try:
            scope = _scope_from(headers, bucketing=str(mem_config.proxy_config().get("session_bucketing") or "day"))
        except Exception as e:  # scope 头是外部输入：非法直接 400，不落到文件系统上
            return JSONResponse(
                status_code=400,
                content={"error": {"message": f"scope 标识非法: {e}", "type": "invalid_scope"}},
            )
        # [A4] cache_control 透传开关：从该账户的**活跃参数快照**读（配置里改了要真的生效）。
        # 它是一个进程级开关（转换器内部状态），代理是单机单账户进程，逐请求设置是安全的。
        try:
            from hippocampus.proxy import format_converters as _fc

            _params = core.active_params(scope)
            _fc.set_cache_control_passthrough(bool(_params.get("cache_control_passthrough", True)))
        except Exception as e:  # 读参数失败不阻断转发（保持转换器上一次的开关值）
            sys.stderr.write(f"[proxy] cache_control 开关读取失败（用默认开）: {e}\n")

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
            try:
                turn = core.consolidate(scope, user_text=user_text)
            except Exception as e:  # 十轮 X1：固化失败不再裸 500，与管理口同口径回 502＋已完成注入段
                return _consolidate_502(scope, injection, e)
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

        # [真流式]（A1）：上游 SSE 逐行转发 → 客户端逐行收到；流末固化＋确认块末尾 delta
        if stream:
            try:
                up_stream = upstream(payload, model=model, endpoint=upstream_kind, stream=True, body=body)
            except UpstreamHTTPError as e:
                # A3：上游非 2xx 原样透传状态码与错误体（流式路径）
                return JSONResponse(status_code=e.status_code, content=e.body)
            except Exception as e:  # 上游异常 → 明确报错，不吞
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": f"上游调用失败: {e}", "type": "upstream_error"}},
                )
            from hippocampus.proxy.streaming import stream_forward

            return StreamingResponse(
                stream_forward(
                    up_stream,
                    inbound=inbound,
                    upstream_kind=upstream_kind,
                    model=model,
                    core=core,
                    scope=scope,
                    user_text=user_text,
                    confirm_text=confirm_text,
                ),
                media_type="text/event-stream",
            )

        try:
            up_body, usage = upstream(payload, model=model, endpoint=upstream_kind, stream=False, body=body)
        except UpstreamHTTPError as e:
            # A3：上游非 2xx 原样透传状态码与错误体（非流式路径）
            return JSONResponse(status_code=e.status_code, content=e.body)
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
        try:
            turn = core.consolidate(scope, user_text=user_text, assistant_text=reply)
        except Exception as e:  # 十轮 X1：固化失败不再裸 500，与管理口同口径回 502＋已完成段
            return _consolidate_502(scope, injection, e, reply=reply)
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

    # ---------- 服务化端点（七轮 T3 / 待拍板 B1 的 E1）：管理口，不是模型口 ----------
    # 与代理形态共用一份应用与一套令牌：`/run` 走"注入＋固化"一轮，`/trace` 取审计。
    # 默认只监听 127.0.0.1（`serve` 的口径），鉴权与 `/v1/*` 同一条 `_authorized`。

    @app.post("/run")
    async def run_turn(request: Request):
        """执行一轮"注入＋提取固化"，返回这一轮的可见决策。

        请求体：`{"text": "…", "assistant_text": "…"?}`（`assistant_text` 只作触发信号，
        模型输出不进正式记忆）。scope 仍按头解析（`X-Hippocampus-Account` 等）。
        """
        if not _authorized(request):
            return JSONResponse(status_code=401, content=_unauthorized_body())
        try:
            body = await read_json_body(request, int(mem_config.proxy_config().get("max_body_bytes") or 0))
        except BodyTooLargeError as e:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {"message": str(e), "type": "body_too_large", "max_body_bytes": e.limit}
                },
            )
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": {"message": f"请求体不是合法 JSON: {e}"}})
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse(status_code=400, content={"error": {"message": "缺少 text（这一轮的输入）"}})
        scope = _scope_from(dict(request.headers))
        injection = core.inject_finalize(scope, text)
        injected = _injected_view(injection)
        dropped = _dropped_view(injection)
        try:
            turn = core.consolidate(
                scope, user_text=text, assistant_text=str((body or {}).get("assistant_text") or "")
            )
        except Exception as e:  # 十轮 X1：固化失败不再裸 500，与管理口同口径回 502＋已完成段
            return _consolidate_502(scope, injection, e, form_hint=" 管理口不转发对话上游，但记忆抽取按配置使用模型端点；配不到可用端点时走规则档。")
        return {
            "run_id": injection.run_id,
            "scope": {"account": scope.account, "session": scope.session},
            "injected": injected,
            "dropped": dropped,
            "note": injection.note,
            "written_ids": list(turn.write.ids),
            "observed_ids": list(turn.observed_ids),
            "pending": turn.pending,
            "confirm_block": turn.confirm_block,
        }

    @app.get("/trace")
    def run_trace(request: Request):
        """按 `run_id` 取那次注入的全链路审计（候选全集＋剔除原因）＋观察事件。

        取数走 `MemoryCore.trace_run()`——形态层不直接读记忆层的日志文件（A22 架构闸）。
        可选 `?observe=run` 把观察事件收窄到该轮的时间窗（默认 `account`＝整个账户，向后兼容）。"""
        if not _authorized(request):
            return JSONResponse(status_code=401, content=_unauthorized_body())
        run_id = (request.query_params.get("run_id") or "").strip()
        if not run_id:
            return JSONResponse(status_code=400, content={"error": {"message": "缺少 run_id（/run 的返回值）"}})
        observe = (request.query_params.get("observe") or "account").strip().lower()
        if observe not in ("account", "run"):
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "observe 只认 account／run", "received": observe}},
            )
        scope = _scope_from(dict(request.headers))
        result = core.trace_run(scope, run_id, observe_granularity=observe)
        if not result["found"]:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"账户 {result['account']} 的审计里没有 run_id={run_id}"
                        "（审计开关关了？还是换过账户？）"
                    }
                },
            )
        return result

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
            # 有上游转换结果就直接用；离线档（converted=None）用响应适配器构造
            # ——此前离线回执只回 {"model": ...}，把正文丢了（实测发现）。
            if converted is not None:
                payload = dict(converted)
            else:
                from hippocampus.proxy.responses_adapter import build_response

                payload = build_response(text, model=model, usage=usage)
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
        from hippocampus.net import validate_endpoint_url
        from hippocampus.proxy.llm_proxy import build_headers, build_upstream_url, stream_upstream
        from hippocampus.settings import llm_post_json

        cfg = mem_config.llm_config()
        if stream:
            # 真流式（A1）：逐行转发上游 SSE。URL 同样过出站校验（A41）；
            # 非 2xx 由 stream_upstream 抛 UpstreamError（状态码 + 错误体）。
            url = build_upstream_url(cfg, endpoint)
            try:
                validate_endpoint_url(url)
            except Exception as e:
                raise RuntimeError(f"上游端点未通过 URL 校验: {e}") from e
            headers = build_headers(cfg)
            return stream_upstream(cfg, url, payload, headers)
        status, data = llm_post_json(payload, endpoint=endpoint)
        if status // 100 != 2:
            # A3：非 2xx 原样透传（状态码 + 上游错误体），不再包成 502
            raise UpstreamHTTPError(status, data)
        return data, (data.get("usage") or {})

    return _upstream


def serve(
    *,
    host: str,
    port: int,
    home: str | Path | None = None,
    confirm_block: bool = True,
    offline: bool = False,
    allow_remote: bool = False,
) -> int:
    """起服务。离线档不需要任何凭据；在线档从环境变量／密钥服务取凭据。

    安全默认（九轮 W1，与 `serve_management` 同口径）：默认只绑环回；要绑非环回必须
    显式 `allow_remote=True` **且**拿得到实例令牌，否则不起。非环回时 `/health` 与
    `/v1/models` 这两个探针口同样要令牌（环回档维持免鉴权）。
    """
    import uvicorn

    loopback = host in _LOOPBACK_HOSTS
    if not loopback and not allow_remote:
        print(f"拒绝启动：代理形态默认只绑环回，收到 host={host}。确认要暴露到非环回请显式加 --allow-remote。")
        return 2
    core = MemoryCore(home=home)
    use_offline = offline or mem_config.is_offline()
    upstream = None if use_offline else make_upstream()
    if not use_offline and not mem_config.endpoint_ready():
        print("（未提供凭据：本进程将按离线档运行——记忆纪律可用，上游转发不可用）")
        upstream = None
    # A2：实例令牌首次启动生成并落盘（之后复用）；doctor 显示前 8 位
    token = mem_config.instance_token(create=True)
    if not token and not loopback:
        print("拒绝启动：非环回绑定必须有实例令牌，但当前环境拿不到令牌文件。")
        core.close()
        return 2
    app = build_app(
        core,
        confirm_block=confirm_block,
        offline=upstream is None,
        upstream=upstream,
        health_requires_auth=not loopback,
        models_requires_auth=not loopback,
    )
    print(f"Hippocampus 代理形态监听 http://{host}:{port}")
    print("  支持三种入站：/v1/chat/completions（OpenAI Chat）、/v1/responses（Responses）、"
          "/v1/messages（Anthropic Messages）")
    print(f"  上游端点按 config 的 llm.api_mode：当前 {mem_config.upstream_endpoint()}")
    if token:
        print(f"  实例令牌已启用（前 8 位 {token[:8]}…）：请求带 `Authorization: Bearer <完整令牌>`")
    else:
        print("  实例令牌：未启用（无令牌环境）")
    print("  把客户端的 base_url 指到 http://host:port 即可；/health 可查格式、端口、锁、索引"
          + ("（非环回：`/health` 与 `/v1/models` 同样要令牌）" if not loopback else "（环回免鉴权）"))
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        core.close()
    return 0


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def serve_management(
    *, host: str = "127.0.0.1", port: int, home: str | Path | None = None, allow_remote: bool = False
) -> int:
    """服务形态（管理口，七轮 T3／E1）：只开 `/run` `/trace` `/health`，不转发上游。

    安全默认（与 A2 同口径，写进 docs/deployment.md）：
    - 只绑环回。要绑到非环回地址必须显式 `allow_remote=True`，**且强制要有实例令牌**
      ——没令牌就不起（管理口能写记忆，暴露到局域网裸奔是不可接受的）；
    - 非环回时 `/health` 同样要令牌（八轮 V8：回体里的计数／索引／锁是实质信息面）。
      环回档维持 0.1.0 以来的免鉴权，便于本机拨测；
    - 与代理形态同一份应用、同一套鉴权，端口也共用（两形态择一起动，不抢端口）。
    """
    import uvicorn

    loopback = host in _LOOPBACK_HOSTS
    if not loopback and not allow_remote:
        print(f"拒绝启动：管理口默认只绑环回，收到 host={host}。确认要暴露到非环回请显式加 --allow-remote。")
        return 2
    core = MemoryCore(home=home)
    try:
        token = mem_config.instance_token(create=True)
        if not token and not loopback:
            print("拒绝启动：非环回绑定必须有实例令牌，但当前环境拿不到令牌文件。")
            return 2
        app = build_app(
            core,
            confirm_block=True,
            offline=True,
            upstream=None,
            auth_token=token or None,
            health_requires_auth=not loopback,
        )
        print(f"Hippocampus 服务形态（管理口）监听 http://{host}:{port}")
        print("  POST /run   执行一轮注入＋固化（body: {\"text\": \"…\"}；scope 走 X-Hippocampus-Account/Session 头）")
        print("  GET  /trace 按 run_id 取那次注入的全链路审计（候选全集＋剔除原因＋观察事件）")
        print("  GET  /health 索引与库健康（嵌入档／计数／锁／索引）"
              + ("（非环回：同样要令牌）" if not loopback else "（环回免鉴权）"))
        if token:
            print(f"  实例令牌已启用（前 8 位 {token[:8]}…）：请求带 `Authorization: Bearer <完整令牌>`")
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        core.close()
    return 0


__all__ = ["CONFIRM_HEADER", "build_app", "make_upstream", "serve", "serve_management"]
