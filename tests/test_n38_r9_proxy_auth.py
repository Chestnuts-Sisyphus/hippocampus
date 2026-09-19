"""九轮 W1：模型口（代理形态）鉴权补齐到与管理口同口径。

八轮 V8 只收紧了**管理口**（`serve_management`）的 `/health`；模型口这边留了三处半成品：
1. `serve()` 从不传 `health_requires_auth` —— 即便 `--host 0.0.0.0`，`/health` 仍匿名可读
   （回体含 `stats`／`index`／`lock`）；
2. `serve()` 没有非环回启动闸 —— 一个 `--host 0.0.0.0` 就把能写记忆的代理挂到局域网；
3. `/v1/models` 免鉴权 —— 回的是**配置的模型名**（装了哪个上游），属信息面。

两态都钉（与 `test_n35_r8_health_auth.py` 同一手法）：
- 接线：假 `uvicorn.run` 截 `build_app` 的关键字参数，真 `serve()` 走完整启动判定；
- 行为：`TestClient` 打同一份应用，验 401／200；
- CLI：`hippocampus proxy --host <非环回>` 必须把 `--allow-remote` 透下去，否则拒起（rc=2）。

不真绑网卡、不起真服务（真 HTTP 那一层在 `scripts/live_proxy_smoke.py`，九轮 W6）。
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from hippocampus.proxy import app as app_mod
from hippocampus.proxy.app import build_app, serve


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _proxy_kwargs(monkeypatch, home, *, host: str, allow_remote: bool) -> dict:
    """跑一遍真实的 `serve()` 接线，截下它传给 build_app 的关键字参数。"""
    recorded: dict = {}
    real_build = app_mod.build_app

    def spy(core, **kwargs):
        recorded.update(kwargs)
        return real_build(core, **kwargs)

    monkeypatch.setattr(app_mod, "build_app", spy)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    rc = serve(host=host, port=_free_port(), home=home, offline=True, allow_remote=allow_remote)
    assert rc == 0
    return recorded


def test_proxy_non_loopback_wires_probe_auth(monkeypatch, home):
    kwargs = _proxy_kwargs(monkeypatch, home, host="0.0.0.0", allow_remote=True)
    assert kwargs.get("health_requires_auth") is True
    assert kwargs.get("models_requires_auth") is True


def test_proxy_loopback_keeps_probe_endpoints_open(monkeypatch, home):
    kwargs = _proxy_kwargs(monkeypatch, home, host="127.0.0.1", allow_remote=False)
    assert kwargs.get("health_requires_auth") is False
    assert kwargs.get("models_requires_auth") is False


def test_proxy_refuses_non_loopback_without_explicit_flag(monkeypatch, home, capsys):
    """缺 `--allow-remote` 时非环回不起：返回 2，且压根不构造应用。"""
    called: list = []
    monkeypatch.setattr(app_mod, "build_app", lambda core, **kwargs: called.append(1))
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: None)
    rc = serve(host="0.0.0.0", port=_free_port(), home=home, offline=True, allow_remote=False)
    assert rc == 2
    assert not called
    assert "--allow-remote" in capsys.readouterr().out


@pytest.fixture()
def token_core(core):
    return core, "h" * 24


def test_models_401_without_token_when_auth_required(token_core):
    core, token = token_core
    client = TestClient(
        build_app(core, offline=True, upstream=None, auth_token=token, models_requires_auth=True)
    )
    assert client.get("/v1/models").status_code == 401
    ok = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    assert ok.json()["data"]


def test_models_stay_open_on_loopback_default(token_core):
    """回归保护：环回档（默认）不带令牌仍 200——收紧只发生在非环回。

    原 `test_a39_proxy_confirm.py` / `test_n15_proxy_tools.py` 的"匿名 200"断言跑在
    免鉴权档（`auth_token=""` 或环回接线）上，与本条同口径，故无需改判；
    真正被 W1 改判的是"非环回仍匿名 200"这一档，由上一条钉成 401。
    """
    core, token = token_core
    open_app = TestClient(build_app(core, offline=True, upstream=None, auth_token=token))
    assert open_app.get("/v1/models").status_code == 200
    secured = TestClient(
        build_app(core, offline=True, upstream=None, auth_token=token, models_requires_auth=True)
    )
    assert secured.get("/v1/models").status_code == 401


def test_health_and_run_auth_unchanged_by_models_flag(token_core):
    """加 `models_requires_auth` 不该顺手改动 `/health`（默认仍免鉴权）与 `/run`（缺令牌 401）。"""
    core, token = token_core
    client = TestClient(
        build_app(core, offline=True, upstream=None, auth_token=token, models_requires_auth=True)
    )
    assert client.get("/health").status_code == 200
    assert client.post("/run", json={"text": "你好"}).status_code == 401


def test_cli_proxy_passes_allow_remote(monkeypatch, home):
    from hippocampus import cli

    recorded: dict = {}

    def fake_serve(**kwargs):
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr(app_mod, "serve", fake_serve)
    rc = cli.main(["--home", str(home), "proxy", "--host", "0.0.0.0", "--allow-remote"])
    assert rc == 0
    assert recorded["host"] == "0.0.0.0"
    assert recorded["allow_remote"] is True


def test_cli_proxy_defaults_allow_remote_false(monkeypatch, home):
    from hippocampus import cli

    recorded: dict = {}
    monkeypatch.setattr(app_mod, "serve", lambda **kwargs: recorded.update(kwargs) or 0)
    cli.main(["--home", str(home), "proxy"])
    assert recorded["allow_remote"] is False
