"""N7 代理鉴权＋上游错误透传（A2/A3）。

验收口径（缺口清单 N7）：
- 实例子令牌：首次启动生成、写配置（instance_token 文件）、`doctor` 只显前 8 位；
- 未带令牌回 401（带对令牌放行）；
- 上游非 2xx 原样透传状态码与错误体（非流式＋流式各一条）。

测试令牌一律在测试内生成（占位），源码零凭据字面量。
"""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from hippocampus.core import Scope
from hippocampus.memory import config as mem_config
from hippocampus.proxy.app import UpstreamHTTPError, build_app


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def _ok_upstream():
    def _upstream(payload, *, model="m", endpoint="chat", stream=False, body=None):
        if stream:
            raise AssertionError("本测试不应走流式")
        return {"choices": [{"message": {"role": "assistant", "content": "上游回复"}}], "usage": {}}, {}

    return _upstream


def _fake_token() -> str:
    """测试占位令牌（非真实凭据）。"""
    return "test-" + secrets.token_hex(16)


def test_instance_token_generated_persisted(home):
    """首次启动生成、落盘、之后复用（令牌稳定）。"""
    import hippocampus.settings as settings

    t1 = settings.instance_token(create=True)
    assert len(t1) >= 32
    assert mem_config.instance_token_path().exists()
    t2 = settings.instance_token(create=True)
    assert t2 == t1  # 复用同一份
    assert mem_config.instance_token_path().read_text(encoding="utf-8").strip() == t1


def test_401_without_token(core, scope):
    """未带令牌 → 401；带对令牌 → 放行。"""
    token = _fake_token()
    app = build_app(core, confirm_block=False, offline=True, upstream=None, auth_token=token)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r.status_code == 401
        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r2.status_code == 200


def test_401_token_from_store(core, home, scope):
    """build_app 默认从 instance_token 存储读取（auth_token=None）。"""
    import hippocampus.settings as settings

    settings.instance_token(create=True)
    token = settings.instance_token()
    app = build_app(core, confirm_block=False, offline=True, upstream=None)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r.status_code == 401
        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r2.status_code == 200


def test_make_upstream_nonstream_path_uses_llm_post_json(core, scope, monkeypatch):
    """`make_upstream` 非流式路径真的走 `llm_post_json`（此前该名未导入——NameError 漏网）。"""
    import hippocampus.settings as settings
    from hippocampus.proxy.app import make_upstream

    captured: dict = {}

    def fake_post(payload, *, endpoint, timeout_s=None):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return 200, {
            "choices": [{"message": {"role": "assistant", "content": "上游回复"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }

    monkeypatch.setattr(settings, "llm_post_json", fake_post)
    upstream = make_upstream()
    body, usage = upstream({"model": "m", "messages": []}, model="m", endpoint="chat", stream=False, body=None)
    assert captured["endpoint"] == "chat"
    assert body["choices"][0]["message"]["content"] == "上游回复"
    assert usage["completion_tokens"] == 2


def test_make_upstream_nonstream_passthrough_on_error(core, scope, monkeypatch):
    """非流式路径：上游非 2xx → 抛 UpstreamHTTPError（带状态码与错误体）。"""
    import hippocampus.settings as settings
    from hippocampus.proxy.app import make_upstream

    monkeypatch.setattr(
        settings, "llm_post_json", lambda payload, *, endpoint, timeout_s=None: (429, {"error": {"message": "限流"}})
    )
    upstream = make_upstream()
    with pytest.raises(UpstreamHTTPError) as ei:
        upstream({"model": "m"}, model="m", endpoint="chat", stream=False, body=None)
    assert ei.value.status_code == 429
    assert ei.value.body["error"]["message"] == "限流"


def test_upstream_error_passthrough_nonstream(core, scope):
    """上游非 2xx 原样透传状态码与错误体（不再包 502）。"""

    def _err_upstream(payload, *, model="m", endpoint="chat", stream=False, body=None):
        if stream:
            raise AssertionError("本测试不应走流式")
        raise UpstreamHTTPError(429, {"error": {"message": "rate limited", "type": "rate_limit"}})

    app = build_app(core, confirm_block=True, offline=False, upstream=_err_upstream)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r.status_code == 429
        assert r.json()["error"]["message"] == "rate limited"


def test_upstream_error_passthrough_stream(core, scope):
    """流式路径：上游非 2xx 原样透传状态码与错误体。"""

    def _err_upstream(payload, *, model="m", endpoint="chat", stream=False, body=None):
        raise UpstreamHTTPError(503, {"error": {"message": "upstream down", "type": "server_error"}})

    app = build_app(core, confirm_block=True, offline=False, upstream=_err_upstream)
    with TestClient(app) as client:
        r = client.post(
            "/v1/chat/completions",
            json={"model": "m", "stream": True, "messages": [{"role": "user", "content": "你好"}]},
        )
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "upstream down"
