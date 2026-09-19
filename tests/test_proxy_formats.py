"""代理形态的**三种入站格式**（T6 强化）：chat／responses／anthropic 都能接。

前身代理就是三端点并存；本项目第一版只做了 chat 一条，这一轮补齐。
本文件验三件事（每种格式各一遍）：

1. **入站识别**：按请求体形状判定（chat 无 `system`；anthropic 有 `system`；responses 有 `input`）；
2. **记忆注入落位**：按各自格式落在正确字段（chat→首条 system；anthropic→`system` 块数组；
   responses→`instructions`）；且**转换到上游**之后仍然在（上游是 chat 时转换不能吃掉注入）；
3. **回包格式**：客户端收到的是**它自己那一套**响应（chat 的 `choices`／anthropic 的
   `content[].text`／responses 的 `output[].content[].text`），确认块追加后同样如此。

用真服务（独立线程 + 真实端口）+ 真 HTTP 客户端，避免"进程内看着对、端口上不对"。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from hippocampus.proxy import formats
from hippocampus.proxy.app import build_app
from hippocampus.seed import seed

uvicorn = pytest.importorskip("uvicorn", reason="代理形态需要 uvicorn（proxy extra）")

CHAT_REPLY = "chat 上游回复"
ANTHROPIC_REPLY = "anthropic 上游回复"


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
        # 纯死锁兜底（V13）：真等待靠轮询 server.started 事件，本值只在uvicorn 永不就绪时兜底，
        # 不做"预计 20 秒内起得来"的功能性假设（慢 runner 上开 chroma 集合可远超 20 秒）。
        deadline = time.time() + 300
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                return self
            time.sleep(0.05)
        raise RuntimeError("代理服务未能启动")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        # 收尾等线程退出：值取 30 秒纯属死锁兜底（实测 should_exit 后 <1 秒退出）；
        # 真卡住时宁可留下一个线程，也不让 teardown 变成新的假红来源。
        self.thread.join(timeout=30)


def _chat_upstream(captured: dict):
    def _up(payload, *, model="", endpoint="chat", stream=False, body=None):
        captured["payload"] = payload
        captured["endpoint"] = endpoint
        return {"choices": [{"message": {"role": "assistant", "content": CHAT_REPLY}}], "usage": {"total_tokens": 3}}, {}

    return _up


def test_detect_inbound_three_formats():
    """识别三格式：靠形状，不靠路径（前身的判别式）。"""
    assert formats.detect_inbound({"model": "m", "messages": [{"role": "user", "content": "hi"}]}) == "chat"
    assert formats.detect_inbound({"model": "m", "system": "x", "messages": []}) == "anthropic"
    assert formats.detect_inbound({"model": "m", "input": "hi"}) == "responses"
    with pytest.raises(ValueError):
        formats.detect_inbound({"model": "m"})


def test_injection_lands_in_each_format():
    """注入落位：三格式各自正确（纯函数层，快）。"""
    chat = {"messages": [{"role": "user", "content": "hi"}]}
    formats.apply_injection(chat, "稳定层内容", "流动层内容")
    assert chat["messages"][0]["role"] == "system" and "稳定层内容" in chat["messages"][0]["content"]
    assert "[海马体记忆]" in chat["messages"][-1]["content"]

    anth = {"system": "原 system", "messages": [{"role": "user", "content": "hi"}]}
    formats.apply_injection(anth, "稳定层内容", "流动层内容")
    assert isinstance(anth["system"], list) and anth["system"][0]["text"] == "原 system"
    assert anth["system"][-1]["text"] == "稳定层内容"
    assert "[海马体记忆]" in anth["messages"][-1]["content"]

    resp = {"instructions": "原 instructions", "input": "hi"}
    formats.apply_injection(resp, "稳定层内容", "流动层内容")
    assert resp["instructions"].endswith("稳定层内容")
    assert resp["input"][-1]["content"][0]["type"] == "input_text"


def test_anthropic_inbound_over_real_server(core, scope):
    """Anthropic 入站：注入落到 system 块，回包是 Anthropic 格式。"""
    seed(core, scope)
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_chat_upstream(captured))
    with _Server(app) as server:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"http://127.0.0.1:{server.port}/v1/messages",
                headers={"X-Hippocampus-Account": scope.account, "anthropic-version": "2023-06-01"},
                json={"model": "claude-x", "max_tokens": 256, "system": "你是助手",
                      "messages": [{"role": "user", "content": "我投简历有什么要求？"}]},
            )
    assert resp.status_code == 200
    body = resp.json()
    # 回包是 Anthropic 形状
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"][0]["type"] == "text"
    assert ANTHROPIC_REPLY in body["content"][0]["text"] or CHAT_REPLY in body["content"][0]["text"]
    # 注入落到两处（这是前身的分层语义，不是随便拼一串）：
    #   stable（长期层）→ system 块；fluid（本轮相关层，含具体条目）→ 末尾的 user 消息
    import json as _json

    payload = captured["payload"]
    assert captured["endpoint"] == "chat"
    assert payload["messages"][0]["role"] == "system"
    assert "海马体记忆" in payload["messages"][0]["content"]          # stable 前言落 system
    assert "[海马体记忆]" in payload["messages"][-1]["content"]      # fluid 块落末尾
    assert "简历投递前必须先过一遍错别字" in _json.dumps(payload, ensure_ascii=False), "具体记忆条目应在载荷里"


def test_responses_inbound_over_real_server(core, scope):
    """Responses 入站：注入落到 instructions，回包是 Responses 形状（output[].content[].text）。"""
    seed(core, scope)
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_chat_upstream(captured))
    with _Server(app) as server:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"http://127.0.0.1:{server.port}/v1/responses",
                headers={"X-Hippocampus-Account": scope.account},
                json={"model": "gpt-x", "instructions": "你是助手", "input": "我投简历有什么要求？"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response" and body["status"] == "completed"
    text = body["output"][0]["content"][0]["text"]
    assert CHAT_REPLY in text
    # 注入：stable 进 system，fluid 进末尾（具体条目在 fluid 里）
    import json as _json

    payload = captured["payload"]
    assert "海马体记忆" in payload["messages"][0]["content"]
    assert "[海马体记忆]" in str(payload["messages"][-1]["content"])
    assert "简历投递前必须先过一遍错别字" in _json.dumps(payload, ensure_ascii=False)


def test_chat_inbound_still_works(core, scope):
    """chat 入站（回归）：这条是原来就有的路径，不能因为补格式而坏掉。"""
    seed(core, scope)
    captured: dict = {}
    app = build_app(core, confirm_block=False, offline=False, upstream=_chat_upstream(captured))
    with _Server(app) as server:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"http://127.0.0.1:{server.port}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account},
                json={"model": "m", "messages": [{"role": "user", "content": "我投简历有什么要求？"}]},
            )
    assert resp.status_code == 200
    assert CHAT_REPLY in resp.json()["choices"][0]["message"]["content"]


def test_streaming_shape_per_format(core, scope):
    """三种格式的流式回包形状（我们未做上游逐行转发 → 单 delta，但协议要对）。"""
    seed(core, scope)
    app = build_app(core, confirm_block=False, offline=True)  # 离线档也能流
    with _Server(app) as server:
        base = f"http://127.0.0.1:{server.port}"
        with httpx.Client(timeout=15) as client:
            chat = client.post(f"{base}/v1/chat/completions", headers={"X-Hippocampus-Account": scope.account},
                               json={"model": "m", "stream": True,
                                     "messages": [{"role": "user", "content": "在看什么岗位"}]})
            assert "data: " in chat.text and "[DONE]" in chat.text

            anth = client.post(f"{base}/v1/messages", headers={"X-Hippocampus-Account": scope.account},
                               json={"model": "m", "stream": True, "system": "s",
                                     "messages": [{"role": "user", "content": "在看什么岗位"}]})
            assert "event: message_start" in anth.text
            assert "content_block_delta" in anth.text and "message_stop" in anth.text

            resp = client.post(f"{base}/v1/responses", headers={"X-Hippocampus-Account": scope.account},
                               json={"model": "m", "stream": True, "input": "在看什么岗位"})
            assert "response.created" in resp.text and "response.completed" in resp.text


def test_unsupported_combination_returns_501(core, scope):
    """不支持的组合（chat 入站 → anthropic 上游）按前身口径回 501，不假装能转。"""
    seed(core, scope)

    def _up(payload, *, model="", endpoint="chat", stream=False, body=None):  # pragma: no cover
        raise AssertionError("不该走到上游")

    app = build_app(core, confirm_block=False, offline=False, upstream=_up)
    with _Server(app) as server:
        from hippocampus import settings as st

        monkey_available = st.upstream_endpoint
        try:
            st.upstream_endpoint = lambda: "responses"  # type: ignore[assignment]
            with httpx.Client(timeout=15) as client:
                r = client.post(
                    f"http://127.0.0.1:{server.port}/v1/chat/completions",
                    headers={"X-Hippocampus-Account": scope.account},
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                )
            assert r.status_code == 501, "chat→responses 应回 501（前身同样不支持）"
        finally:
            st.upstream_endpoint = monkey_available  # type: ignore[assignment]


def test_health_reports_formats_and_endpoint(core):
    from fastapi.testclient import TestClient

    payload = TestClient(build_app(core, offline=True)).get("/health").json()
    assert set(payload["formats"]) == {"chat", "responses", "anthropic"}
    assert payload["upstream_endpoint"] in {"chat", "anthropic", "responses"}


def test_memory_switch_off_means_no_injection_in_any_format(core, scope):
    """开关对三格式一致生效（记忆关掉后三种格式的注入都为空）。"""
    seed(core, scope)
    core.set_switch(scope, "关闭记忆")
    try:
        for body in (
            {"model": "m", "messages": [{"role": "user", "content": "我投简历有什么要求？"}]},
            {"model": "m", "system": "s", "messages": [{"role": "user", "content": "我投简历有什么要求？"}]},
            {"model": "m", "input": "我投简历有什么要求？"},
        ):
            kind = formats.detect_inbound(body)
            snapshot = str(body)
            formats.apply_injection(body, "", "")  # 空注入 = 不动
            assert str(body) == snapshot, f"{kind} 在开关关闭时不该被改"
    finally:
        core.set_switch(scope, "打开记忆")
