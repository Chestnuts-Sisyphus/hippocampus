"""十轮 X1/X3：模型口固化 502 口径一致性与请求体体积上限闸。

实现已提交（批 A，commit `87447ea`）；本文件补全其验收测试（前会话残桩在写入 docstring 时中断）。

判据：
- X1：模型口（chat 固化）与管理口（/run）遇"固化阶段抛错"必须**同一状态码 502、同一形状**，
  并回带已完成注入段（`error.stage=consolidate`＋`injected`/`dropped`/`run_id`/`scope`）——不再是裸 500；
- X3：请求体超 `proxy.max_body_bytes`（env `HIPPOCAMPUS_MAX_BODY_BYTES`，默认 10 MiB）→ 413
  `body_too_large`；limit=0 ＝"不承诺上限"，大 body 不得被挡。

零出站（offline 档＋monkeypatch），不依赖任何凭据。
"""

from __future__ import annotations

import pytest

from hippocampus.proxy.app import build_app

pytest.importorskip("fastapi", reason="代理形态需要 proxy extra")


def _boom(*args, **kwargs):
    raise ValueError("上游回体不是合法 JSON（测试注入）")


def _client(core, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(core, "consolidate", _boom)
    return TestClient(build_app(core, confirm_block=False, offline=True, auth_token=""))


def test_model_port_consolidate_returns_502_with_injected(core, scope, monkeypatch):
    """X1：模型口固化失败 → 502＋stage＋已完成注入段（判据原文：不许裸 500）。"""
    client = _client(core, monkeypatch)
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-Hippocampus-Account": scope.account},
        json={"model": "m", "messages": [{"role": "user", "content": "我喜欢咖啡"}]},
    )
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"]["stage"] == "consolidate"
    assert body["error"]["type"] == "consolidation_failed"
    for key in ("run_id", "scope", "injected", "dropped", "upstream_reply"):
        assert key in body, f"502 回体缺已完成段：{key}"
    assert body["scope"]["account"] == scope.account


def test_management_port_same_shape(core, scope, monkeypatch):
    """X1 两形态一致判据：管理口 /run 同错同码同形状，且带管理口 form_hint 可分辨。"""
    client = _client(core, monkeypatch)
    resp = client.post("/run", headers={"X-Hippocampus-Account": scope.account}, json={"text": "我喜欢咖啡"})
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"]["stage"] == "consolidate"
    assert "管理口" in body["error"]["message"]


def test_body_too_large_413(core, monkeypatch):
    """X3：content-length 超上限 → 413，回体带 max_body_bytes。"""
    monkeypatch.setenv("HIPPOCAMPUS_MAX_BODY_BYTES", "200")
    from fastapi.testclient import TestClient

    client = TestClient(build_app(core, confirm_block=False, offline=True, auth_token=""))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "x" * 5000}]},
    )
    assert resp.status_code == 413, resp.text
    err = resp.json()["error"]
    assert err["type"] == "body_too_large"
    assert err["max_body_bytes"] == 200


def test_limit_zero_means_no_cap(core, monkeypatch):
    """X3 边界：limit=0 ＝运维显式声明不承诺上限（docs/proxy.md §三·五口径），大 body 不得 413。"""
    monkeypatch.setenv("HIPPOCAMPUS_MAX_BODY_BYTES", "0")
    from fastapi.testclient import TestClient

    client = TestClient(build_app(core, confirm_block=False, offline=True, auth_token=""))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "x" * 30000}]},
    )
    assert resp.status_code != 413, "limit=0 仍被挡 → 不承诺上限口径失效"
