"""验收 T6（强化版）：**真起服务**，用真 HTTP 客户端走一遍"改 base_url 即获得记忆"。

为什么单独一个文件：`tests/test_a39_proxy_confirm.py` 用的是 FastAPI TestClient（进程内，
不经过真实 socket）。但"改一行 base_url 就生效"这句话的**关键前提是它真的在监听端口**——
所以这里起真的 uvicorn（独立线程 + 真实端口），用 httpx 当外部客户端连上去，
断言：① 记忆被注入到转发给上游的 messages 里；② 上游回复 + 记忆层追加的确认块一起回到客户端；
③ 上游收到的 messages 里**能看到注入文本本身**（不是"看起来像有记忆"）。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from hippocampus.proxy.app import build_app
from hippocampus.seed import seed

uvicorn = pytest.importorskip("uvicorn", reason="代理形态需要 uvicorn（proxy extra）")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """在后台线程里起一个真 uvicorn，退出时干净收尾。"""

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


def test_real_uvicorn_serves_and_injects(core, scope):
    """真端口 + 真客户端：注入 → 转发 → 追加确认块 → 回到客户端。"""
    seed(core, scope)
    captured: dict[str, object] = {}

    def upstream(payload, *, model="", endpoint="chat", stream=False, body=None):
        # 转发函数的契约：收"已转换好的上游请求体"（chat 入站 → chat 上游＝原样），
        # 返回 (上游响应体 dict, usage)
        captured["payload"] = payload
        captured["endpoint"] = endpoint
        return {"choices": [{"message": {"role": "assistant", "content": "上游的回复正文"}}],
                "usage": {"total_tokens": 7}}, {"total_tokens": 7}

    app = build_app(core, confirm_block=True, offline=False, upstream=upstream)
    with _Server(app) as server:
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}/v1", timeout=15)
        health = client.get(f"http://127.0.0.1:{server.port}/health").json()
        assert health["ok"] is True

        response = client.post(
            "/chat/completions",
            headers={"X-Hippocampus-Account": scope.account},
            json={"model": "any", "messages": [{"role": "user", "content": "我投简历有什么要求？"}]},
        )
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        client.close()

    # ① 上游确实收到了注入过的 messages
    forwarded = (captured.get("payload") or {}).get("messages")
    assert isinstance(forwarded, list) and forwarded, "上游应收到消息"
    blob = "\n".join(str(m.get("content") or "") for m in forwarded)
    assert "简历投递前必须先过一遍错别字" in blob, "记忆必须注入到转发给上游的消息里"
    assert any(str(m.get("role")) == "system" for m in forwarded), "注入以 system 消息前置"

    # ② 客户端拿到的是上游回复（+ 可能的确认块），不是离线回执
    assert "上游的回复正文" in content, "客户端应收到上游回复"
    assert "离线档" not in content


def test_real_server_confirm_block_is_appended_and_switchable(core, scope):
    """确认块由记忆层生成、代理追加；关掉开关就不追加。"""
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")

    def upstream_ok(payload, *, model="", endpoint="chat", stream=False, body=None):
        return {"choices": [{"message": {"role": "assistant", "content": "上游回复"}}], "usage": {}}, {}

    with _Server(build_app(core, confirm_block=True, offline=False, upstream=upstream_ok)) as server:
        base = f"http://127.0.0.1:{server.port}"
        with httpx.Client(timeout=15) as client:
            # 触发冲突 → 代理把 pending 确认块追加到回复
            r1 = client.post(
                f"{base}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account},
                json={"messages": [{"role": "user", "content": "我的期望城市是南京"}]},
            )
            assert r1.status_code == 200
            text1 = r1.json()["choices"][0]["message"]["content"]
            assert "记忆·确认" in text1, "有冲突时确认块应被系统追加"

            # 关掉开关（请求头声明）
            r2 = client.post(
                f"{base}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account, "X-Hippocampus-Confirm": "0"},
                json={"messages": [{"role": "user", "content": "再随便说一句"}]},
            )
            text2 = r2.json()["choices"][0]["message"]["content"]
            assert "记忆·确认" not in text2, "开关关闭时不该追加确认块"

            # `确认 n` 在入口被消费（不转发给上游）
            pending = core.pending(scope)
            assert pending, "应有未决确认"
            candidate = next(p for p in pending if p["is_new"])
            r3 = client.post(
                f"{base}/v1/chat/completions",
                headers={"X-Hippocampus-Account": scope.account},
                json={"messages": [{"role": "user", "content": f"确认{candidate['num']}"}]},
            )
            assert "已确认" in r3.json()["choices"][0]["message"]["content"]


def test_real_server_health_reports_port_and_lock(core, scope):
    with _Server(build_app(core, offline=True)) as server:
        with httpx.Client(timeout=15) as client:
            payload = client.get(f"http://127.0.0.1:{server.port}/health").json()
    assert payload["port"] == 8765, "配置的端口应被报告（这里读的是配置值，不是监听端口）"
    assert "lock" in payload and payload["lock"]["path"]
    assert set(payload["stats"]) >= {"memories", "entities", "episodes", "relations"}
