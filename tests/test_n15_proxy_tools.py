"""N15 代理线收口（A5/A4/A6/A7）：带 tools 的三向端到端 ＋ 三个小缺口。

验收口径（缺口清单 N15）：
① 带 `tools` 的 chat／anthropic／responses **三向各一条真服务端到端**（此前只有转换器单测，
   没有"真请求 + 真工具字段"的联调）；
② `cache_control_passthrough` **从活跃参数生效**（此前只在默认值里，没有任何生产路径调用）；
③ `/v1/models` 回**配置的模型名**（不再回占位串）；
④ session 分桶策略**可配**（`proxy.session_bucketing`：day／hour／none）。

真服务（uvicorn 线程 + 真 HTTP 客户端），与 test_proxy_formats.py 同一套方法。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest

from hippocampus.memory import database as db
from hippocampus.proxy.app import _scope_from, build_app

uvicorn = pytest.importorskip("uvicorn", reason="代理形态需要 uvicorn（proxy extra）")

CHAT_REPLY = "chat 上游回复"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    def __init__(self, app) -> None:
        self.port = _free_port()
        self.config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("代理服务未能启动")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _capturing_upstream(captured: dict):
    def _up(payload, *, model="", endpoint="chat", stream=False, body=None):
        captured["payload"] = payload
        captured["endpoint"] = endpoint
        return {"choices": [{"message": {"role": "assistant", "content": CHAT_REPLY}}], "usage": {}}, {}

    return _up


# ---------------------------------------------------------------- ① tools 三向


def test_tools_chat_inbound_end_to_end(core, scope):
    """chat 入站 + tools：工具定义原样到上游，回包带 tool_calls 也能回给客户端。"""
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_capturing_upstream(captured))
    tools = [
        {
            "type": "function",
            "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    with _Server(app) as server, httpx.Client(timeout=15) as client:
        resp = client.post(
            f"http://127.0.0.1:{server.port}/v1/chat/completions",
            headers={"X-Hippocampus-Account": scope.account},
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "北京天气？"}],
                "tools": tools,
                "tool_choice": "auto",
            },
        )
    assert resp.status_code == 200, resp.text
    payload = captured["payload"]
    assert payload["tools"] == tools, "chat→chat 的工具定义应原样透传"
    assert payload.get("tool_choice") == "auto"
    assert resp.json()["choices"][0]["message"]["content"] == CHAT_REPLY


def test_tools_anthropic_inbound_end_to_end(core, scope):
    """anthropic 入站 + tools：`input_schema` 形状转换成 chat 的 `function.parameters`。"""
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_capturing_upstream(captured))
    with _Server(app) as server, httpx.Client(timeout=15) as client:
        resp = client.post(
            f"http://127.0.0.1:{server.port}/v1/messages",
            headers={"X-Hippocampus-Account": scope.account, "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-x",
                "max_tokens": 256,
                "system": "你是助手",
                "messages": [{"role": "user", "content": "北京天气？"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "查天气",
                        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                    }
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    payload = captured["payload"]
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "get_weather"
    assert "city" in json.dumps(payload["tools"][0]["function"]["parameters"], ensure_ascii=False)
    body = resp.json()
    assert body["type"] == "message" and CHAT_REPLY in body["content"][0]["text"]


def test_tools_responses_inbound_end_to_end(core, scope):
    """responses 入站 + tools：转成 chat 的 function 形状，且回包是 Responses 形状。"""
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_capturing_upstream(captured))
    with _Server(app) as server, httpx.Client(timeout=15) as client:
        resp = client.post(
            f"http://127.0.0.1:{server.port}/v1/responses",
            headers={"X-Hippocampus-Account": scope.account},
            json={
                "model": "gpt-x",
                "instructions": "你是助手",
                "input": "北京天气？",
                "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            },
        )
    assert resp.status_code == 200, resp.text
    payload = captured["payload"]
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "get_weather"
    body = resp.json()
    assert body["output"][0]["content"][0]["type"] == "output_text"


# ------------------------------------------------ ② cache_control 从参数生效


def _long_text(n: int = 2400) -> str:
    return "记忆项目说明。" * (n // 7)


def _anthropic_shaped_upstream(captured: dict):
    """上游按 anthropic 形状回（这条链路要的是 anthropic_messages 上游）。"""

    def _up(payload, *, model="", endpoint="anthropic", stream=False, body=None):
        captured["payload"] = payload
        return (
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "上游回复"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            {},
        )

    return _up


def _post_chat_to_anthropic(core, scope, captured) -> dict:
    """chat 入站 → anthropic 上游：这条方向**保留块结构**，才能看出 cache_control 通不通。

    （anthropic→chat 方向会把 system 拍平成字符串，cache_control 必然丢——那是格式本身的
    语义，不适合拿来测这个开关。）
    """
    from hippocampus import settings as mem_config

    original = mem_config.upstream_endpoint
    mem_config.upstream_endpoint = lambda: "anthropic"  # type: ignore[assignment]
    try:
        app = build_app(core, confirm_block=False, offline=False, upstream=_anthropic_shaped_upstream(captured))
        with _Server(app) as server, httpx.Client(timeout=15) as client:
            resp = client.post(
                f"http://127.0.0.1:{server.port}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account},
                json={
                    "model": "m",
                    "messages": [
                        {
                            "role": "system",
                            "content": [
                                {"type": "text", "text": _long_text(), "cache_control": {"type": "ephemeral"}}
                            ],
                        },
                        {"role": "user", "content": "你好"},
                    ],
                },
            )
    finally:
        mem_config.upstream_endpoint = original  # type: ignore[assignment]
    assert resp.status_code == 200, resp.text
    return captured["payload"]


def _set_param(core, scope, **updates) -> None:
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        db.set_active_params(session.conn, updates, reason="N15 测试")


def test_cache_control_passthrough_on_by_default(core, scope):
    """默认开：入站的 cache_control 字段一路保留到上游（此前开关没人调用，等于整条路径没人管）。"""
    captured: dict = {}
    payload = _post_chat_to_anthropic(core, scope, captured)
    blocks = payload["system"]
    assert any("cache_control" in json.dumps(b, ensure_ascii=False) for b in blocks), (
        f"默认应保留 cache_control（透传开关默认 True），实际 system 块 {blocks[:2]}"
    )


def test_cache_control_passthrough_off_from_params(core, scope):
    """把活跃参数里的开关关掉 → 上游载荷里的 cache_control 被剥掉（A4：配置真的生效）。"""
    _set_param(core, scope, cache_control_passthrough=False)
    try:
        captured: dict = {}
        payload = _post_chat_to_anthropic(core, scope, captured)
        assert "cache_control" not in json.dumps(payload, ensure_ascii=False), "关掉后不该还有 cache_control"
    finally:
        _set_param(core, scope, cache_control_passthrough=True)


# ------------------------------------------------------ ③ /v1/models 回配置名


def test_models_endpoint_returns_configured_model(core, monkeypatch):
    monkeypatch.setenv("HIPPOCAMPUS_MODEL", "my-configured-model")
    app = build_app(core, confirm_block=False, offline=True)
    with _Server(app) as server, httpx.Client(timeout=15) as client:
        resp = client.get(f"http://127.0.0.1:{server.port}/v1/models")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()["data"]]
    assert ids == ["my-configured-model"], f"应回配置的模型名，实际 {ids}"


# ------------------------------------------------------- ④ session 分桶可配


def test_session_bucketing_modes():
    day = _scope_from({}, bucketing="day").session
    hour = _scope_from({}, bucketing="hour").session
    none = _scope_from({}, bucketing="none").session
    assert day.startswith("day-") and len(day) == len("day-YYYYMMDD")
    assert hour.startswith("hour-") and len(hour) == len("hour-YYYYMMDDHH")
    assert none == "default"
    # 显式传 session 头时**永远以客户端为准**（分桶只管缺省）
    assert _scope_from({"x-hippocampus-session": "my-sess"}, bucketing="none").session == "my-sess"
    assert _scope_from({"X-Session-Id": "other"}, bucketing="day").session == "other"


def test_session_bucketing_from_config(core, scope):
    """配置项真的接到请求入口：`proxy.session_bucketing=none` → 请求落的经历会话名是 `session_default`
    （默认档下则是 `session_day-YYYYMMDD`）——分桶策略确实可配，不是只写在文档里。"""
    from hippocampus import settings as mem_config

    def _up(payload, *, model="", endpoint="chat", stream=False, body=None):
        return {"choices": [{"message": {"role": "assistant", "content": CHAT_REPLY}}], "usage": {}}, {}

    def _post() -> None:
        app = build_app(core, confirm_block=False, offline=False, upstream=_up)
        with _Server(app) as server, httpx.Client(timeout=15) as client:
            resp = client.post(
                f"http://127.0.0.1:{server.port}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account},
                json={"model": "m", "messages": [{"role": "user", "content": "我不看外包"}]},
            )
        assert resp.status_code == 200, resp.text

    original = mem_config.proxy_config

    def _patched(mode: str):
        def _inner():
            cfg = dict(original())
            cfg["session_bucketing"] = mode
            return cfg

        return _inner

    # ① 默认档（day）：经历落在 session_day-YYYYMMDD
    _post()
    # ② 配成 none：经历落在 session_default
    mem_config.proxy_config = _patched("none")  # type: ignore[assignment]
    try:
        _post()
    finally:
        mem_config.proxy_config = original  # type: ignore[assignment]

    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        rows = session.conn.execute("SELECT DISTINCT session_id FROM episodes").fetchall()
    names = {r["session_id"] for r in rows}
    assert "session_default" in names, f"配 none 时应落到固定 session，实际 {names}"
    assert any(n.startswith("session_day-") for n in names), f"默认档应落到按天分桶，实际 {names}"
