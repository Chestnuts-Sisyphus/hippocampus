"""九轮 W10（K15）：`/trace` 的 `observe[]` 粒度收口——账户粒度写清，run 粒度真的收窄。

缺口本来的样子：`observe` 数组名义上是"这一次 run 的观察事件"，实际取数条件是
"`run_id` 相等 **或** 事件属于 verification/confirmation"，而那三类观察事件**都不写 run_id**
（写它们在记忆层，够不到核心生成的 run_id）→ 等价于"把整个账户的同类事件都给你"。
八轮 V9 只是把这条事实写进文档，没有可用的收窄手段，所以 `run_id` 粒度的审计里
夹着一堆别的轮次的事件。

本轮落地"支持 run_id 过滤（默认向后兼容）"这一支：
- 默认 `observe=account` 行为一字不改（老调用方拿到的东西不变）；
- `observe=run` 在默认结果之上**再按该轮时间窗收窄**（只会更少，不会反过来变多）；
- 非法粒度在边界上就 400，不进核心。

测试手法：真起一台离线服务（`TestClient`）跑一轮 `/run` 拿真 run_id 与真审计时间戳，
再往 `observe.jsonl` 手工落"窗口内／窗口外"两条事件——**只有这样才能证明过滤真的生效**，
光看代码等于没看。窗口外的事件用一小时前，与 `OBSERVE_WINDOW_PAD_MS` 的余量相差三个数量级，
不会因为机器慢而翻红。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hippocampus.memory import jsonl_log, observe_log
from hippocampus.proxy.app import build_app
from hippocampus.seed import seed

TOKEN = "t" * 24
OUT_OF_WINDOW_MS = 3_600_000  # 一小时以前——远超时间窗余量，判"窗口外"不会因机器慢而翻红


def _headers(account_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Hippocampus-Account": account_id, "content-type": "application/json"}


def _write_observe(account_id: str, status: str, ts: int) -> None:
    """手工落一条 verification 观察事件（默认粒度就是要把它带出来，正合适当靶子）。"""
    jsonl_log.append_jsonl(
        observe_log.observe_path(account_id),
        {"event": "verification", "ts": ts, "status": status, "method": "test", "evidence": "", "dropped": False},
    )


@pytest.fixture()
def served(core, scope, home):
    seed(core, scope)
    app = build_app(core, confirm_block=True, offline=True, upstream=None, auth_token=TOKEN)
    with TestClient(app) as client:
        yield client, scope


def _run_id_and_audit_ts(client, scope) -> tuple[str, int]:
    run = client.post("/run", headers=_headers(scope.account), json={"text": "我的目标岗位方向是什么？"})
    assert run.status_code == 200, run.text
    run_id = run.json()["run_id"]
    trace = client.get("/trace", headers=_headers(scope.account), params={"run_id": run_id})
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["audit"], "这一轮没有审计事件，后面的窗口断言无从谈起"
    return run_id, int(body["audit"][0]["ts"])


def test_default_granularity_is_account_and_unchanged(served):
    """默认粒度＝account，且**不传 observe 时**行为与八轮一致（向后兼容的正面证据）。"""
    client, scope = served
    run_id, audit_ts = _run_id_and_audit_ts(client, scope)
    _write_observe(scope.account, "in-window", audit_ts)
    _write_observe(scope.account, "long-ago", audit_ts - OUT_OF_WINDOW_MS)

    body = client.get("/trace", headers=_headers(scope.account), params={"run_id": run_id}).json()
    assert body["observe_granularity"] == "account"
    # 十轮订正：批 B（X2）后账户级 observe 合法多出**无 status 的 injection 事件**（run_id 穿进写入点），
    # 本测试的靶子是手工 verification 事件，按事件类型过滤再断言，不针对新事件种类假装它们不存在。
    verification_events = [e for e in body["observe"] if e.get("event") == "verification"]
    assert {e["status"] for e in verification_events} == {"in-window", "long-ago"}, (
        "默认粒度本该是账户级全量，收窄了就是改了对外行为"
    )


def test_observe_run_granularity_narrows_to_that_run(served):
    """`observe=run` 必须把窗口外那轮的事件滤掉——这才是 K15 要的"过滤生效"。"""
    client, scope = served
    run_id, audit_ts = _run_id_and_audit_ts(client, scope)
    _write_observe(scope.account, "in-window", audit_ts)
    _write_observe(scope.account, "long-ago", audit_ts - OUT_OF_WINDOW_MS)

    account_events = client.get("/trace", headers=_headers(scope.account), params={"run_id": run_id}).json()["observe"]
    body = client.get("/trace", headers=_headers(scope.account), params={"run_id": run_id, "observe": "run"}).json()
    assert body["observe_granularity"] == "run"
    # 同上订正：run 粒度下对**手工 verification 事件**（无 run_id，落不进窗口）的判据不变
    assert [e["status"] for e in body["observe"] if e.get("event") == "verification"] == ["in-window"]
    assert len(body["observe"]) <= len(account_events), "run 粒度只会更少，不能比默认还多"


def test_invalid_granularity_is_a_400_at_the_boundary(served):
    client, scope = served
    run_id, _ts = _run_id_and_audit_ts(client, scope)
    resp = client.get("/trace", headers=_headers(scope.account), params={"run_id": run_id, "observe": "随便一个值"})
    assert resp.status_code == 400
    assert "account" in resp.json()["error"]["message"]


def test_core_rejects_unknown_granularity(core, scope):
    """记忆层也不兜空：未知粒度直接抛，避免拼错的参数被静默当成默认值。"""
    with pytest.raises(ValueError, match="account"):
        core.trace_run(scope, "run-1", observe_granularity="whatever")
