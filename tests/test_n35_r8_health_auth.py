"""八轮 V8：管理口绑到**非环回**时 `/health` 也要令牌（环回档行为不变）。

背景（七轮 B8 的残余面）：`/health` 自 0.1.0 起免鉴权，回体里有 `stats`／`index`／`lock`
——本机拨测方便，但 `--allow-remote` 时它就等于把库体量与索引健康挂到局域网。
`serve_management` 原先只校验"非环回必须有令牌"，没把令牌用到健康口上。

两态都钉：
- 接线：`serve_management(host=非环回, allow_remote=True)` 必须把 `health_requires_auth=True` 传下去；
- 行为：该标志为真时不带令牌 401、带令牌 200；为假（环回默认）时不带令牌仍 200。

不起真服务器、也不真绑 `0.0.0.0`：接线用假 `uvicorn.run` 截参，行为用 `TestClient` 打同一份应用。
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from hippocampus.core import MemoryCore
from hippocampus.proxy import app as app_mod
from hippocampus.proxy.app import build_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _management_kwargs(monkeypatch, home, *, host: str, allow_remote: bool) -> dict:
    """跑一遍真实的 `serve_management` 接线，截下它传给 build_app 的关键字参数。"""
    recorded: dict = {}
    real_build = app_mod.build_app

    def spy(core: MemoryCore, **kwargs):
        recorded.update(kwargs)
        return real_build(core, **kwargs)

    monkeypatch.setattr(app_mod, "build_app", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    rc = app_mod.serve_management(host=host, port=_free_port(), home=home, allow_remote=allow_remote)
    assert rc == 0
    return recorded


def test_non_loopback_wires_health_auth(monkeypatch, home):
    kwargs = _management_kwargs(monkeypatch, home, host="0.0.0.0", allow_remote=True)
    assert kwargs.get("health_requires_auth") is True


def test_loopback_keeps_health_open_by_default(monkeypatch, home):
    kwargs = _management_kwargs(monkeypatch, home, host="127.0.0.1", allow_remote=False)
    assert kwargs.get("health_requires_auth") is False


@pytest.fixture()
def token_core(core):
    return core, "h" * 24


def test_health_401_without_token_when_auth_required(token_core):
    core, token = token_core
    client = TestClient(build_app(core, offline=True, upstream=None, auth_token=token, health_requires_auth=True))
    assert client.get("/health").status_code == 401
    ok = client.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True and "index" in body and "stats" in body


def test_health_stays_open_on_loopback_default(token_core):
    """回归保护：默认（环回）档不带令牌仍 200，收紧只发生在非环回。"""
    core, token = token_core
    open_app = TestClient(build_app(core, offline=True, upstream=None, auth_token=token))
    assert open_app.get("/health").status_code == 200
    secured = TestClient(build_app(core, offline=True, upstream=None, auth_token=token, health_requires_auth=True))
    assert secured.get("/health").status_code == 401


def test_run_and_trace_auth_unchanged_by_the_flag(token_core):
    """`/run` `/trace` 本来就过同一条 `_authorized`，加标志不该改变它们（也不该放行）。"""
    core, token = token_core
    client = TestClient(build_app(core, offline=True, upstream=None, auth_token=token, health_requires_auth=True))
    assert client.post("/run", json={"text": "你好"}).status_code == 401
    assert client.get("/trace", params={"run_id": "x"}).status_code == 401
    ok = client.post("/run", json={"text": "我的目标岗位方向是什么？"},
                     headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
