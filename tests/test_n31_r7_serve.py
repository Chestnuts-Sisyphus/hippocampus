"""七轮 T3 验收：服务形态（E1）三端点 `/run` `/trace` `/health` 的**真服务器**测试。

为什么起真 uvicorn 而不是 TestClient：待拍板 B1 的验收口径写的是"pytest 真服务器测试
覆盖三端点"，而服务形态的一句话（"管理口只绑环回＋实例令牌"）只有在真监听、真带
`Authorization` 头时才成立。复用 `test_t6_real_server.py` 的起服务/收尾套路。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from hippocampus.proxy.app import build_app, serve_management
from hippocampus.seed import seed

uvicorn = pytest.importorskip("uvicorn", reason="服务形态需要 uvicorn（proxy extra）")


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
        raise RuntimeError("服务形态未能启动")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def served(core, scope, home):
    """真起一台离线服务（带实例令牌），返回 (base_url, headers, server-context)。"""
    seed(core, scope)
    token = "t" * 24
    app = build_app(core, confirm_block=True, offline=True, upstream=None, auth_token=token)
    with _Server(app) as srv:
        yield f"http://127.0.0.1:{srv.port}", {"Authorization": f"Bearer {token}"}, scope


def test_health_reports_index_and_store(served):
    """`/health` 回索引与库健康（服务化口径），不是只有"进程活着"。"""
    base, headers, scope = served
    resp = httpx.get(f"{base}/health", headers=headers, timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["offline"] is True
    assert "index" in body, "/health 缺索引健康"
    assert body["embedding"]["model"], "缺嵌入档"
    assert isinstance(body["stats"], dict)


def test_run_executes_one_turn_and_returns_run_id(served):
    """`/run` 跑一轮注入＋固化：回 run_id、注入内容、本轮写库的 id。"""
    base, headers, scope = served
    resp = httpx.post(
        f"{base}/run",
        headers={**headers, "content-type": "application/json", "X-Hippocampus-Account": scope.account},
        json={"text": "我找岗位时有哪些硬性限制？"},
        timeout=20,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"], "没回 run_id"
    contents = " ".join(i["content"] for i in body["injected"])
    assert "远程" in contents or "出差" in contents, f"该注入的记忆没进来：{contents!r}"
    assert body["scope"]["account"] == scope.account


def test_run_requires_text_and_token(served):
    """缺 text → 400；缺令牌 → 401（与 `/v1/*` 同一条鉴权口径）。"""
    base, headers, scope = served
    hdrs = {**headers, "X-Hippocampus-Account": scope.account, "content-type": "application/json"}
    bad = httpx.post(f"{base}/run", headers=hdrs, json={"text": "   "}, timeout=10)
    assert bad.status_code == 400
    noauth = httpx.post(f"{base}/run", headers={"content-type": "application/json"}, json={"text": "你好"}, timeout=10)
    assert noauth.status_code == 401
    notoken_trace = httpx.get(f"{base}/trace", params={"run_id": "x"}, timeout=10)
    assert notoken_trace.status_code == 401


def test_trace_returns_full_candidates_for_that_run(served):
    """`/trace?run_id=` 拿回那次注入的审计（候选全集＋注入标记＋剔除原因）。"""
    base, headers, scope = served
    hdrs = {**headers, "X-Hippocampus-Account": scope.account, "content-type": "application/json"}
    run = httpx.post(f"{base}/run", headers=hdrs, json={"text": "我的目标岗位方向是什么？"}, timeout=20)
    run_id = run.json()["run_id"]

    trace = httpx.get(f"{base}/trace", headers=hdrs, params={"run_id": run_id}, timeout=20)
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["run_id"] == run_id
    assert body["audit"], "该 run 没有审计事件"
    event = body["audit"][0]
    assert "candidates" in event, "审计里没有候选全集（A12 口径）"
    assert any(c.get("injected") for c in event["candidates"]), "候选里没有任何被注入项"

    missing = httpx.get(f"{base}/trace", headers=hdrs, params={"run_id": "run_不存在"}, timeout=20)
    assert missing.status_code == 404


def test_trace_requires_run_id(served):
    base, headers, _scope = served
    resp = httpx.get(f"{base}/trace", headers=headers, timeout=10)
    assert resp.status_code == 400


def test_management_port_refuses_non_loopback_by_default(tmp_path, capsys):
    """安全默认：管理口不绑非环回，除非显式 `allow_remote`（且必须有令牌）。"""
    rc = serve_management(host="0.0.0.0", port=_free_port(), home=tmp_path / "h")
    assert rc == 2
    assert "只绑环回" in capsys.readouterr().out


def test_run_degrades_to_502_when_consolidation_fails(core, served, monkeypatch):
    """真机冒烟（09-19）钉住的缺陷：机器上存着失效模型凭据时，`/run` 的固化阶段会抛异常。

    旧行为：异常直穿 ASGI → 裸 500，调用方看不到断在哪一段；
    现在：**502** ＋ 已完成的注入结果（`run_id`／`injected`／`dropped`）一起回。
    """

    def boom(*args, **kwargs):
        raise RuntimeError("401 Authorization Required for url 'https://api.example.com'")

    monkeypatch.setattr(core, "consolidate", boom)
    base, headers, scope = served
    resp = httpx.post(
        f"{base}/run",
        headers={**headers, "content-type": "application/json", "X-Hippocampus-Account": scope.account},
        json={"text": "我找岗位时有哪些硬性限制？"},
        timeout=20,
    )
    assert resp.status_code == 502, f"固化失败不该裸 500：{resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert body["error"]["stage"] == "consolidate"
    assert body["run_id"], "502 也得把已完成的注入段 run_id 回出来"
    assert "injected" in body and "dropped" in body
