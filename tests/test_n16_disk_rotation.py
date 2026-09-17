"""N16 磁盘与开销（D1）：audit／observe JSONL 的大小轮转。

验收口径（缺口清单 N16）：
① `audit.jsonl` / `observe.jsonl` **到上限会滚动**（`.1`／`.2`…），保留份数可配；
② 滚动**不丢最新事件**（读当前文件仍能拿到刚写的那条）；
③ `load_events(include_rotated=True)` 能把滚动份一起读回来（历史可复盘）；
④ 上限/份数来自配置 `observability.jsonl_max_bytes` / `jsonl_keep`；
⑤ 轮转失败**软失败**（不抛、不阻断注入/固化）。
"""

from __future__ import annotations

import json

from hippocampus.memory import audit, jsonl_log, observe_log


def _cfg(home, *, max_bytes: int, keep: int) -> None:
    """把轮转参数写进该数据根的 config.json（走项目自己的配置路径）。"""
    conf = home / "config.json"
    data = {}
    if conf.exists():
        data = json.loads(conf.read_text(encoding="utf-8") or "{}")
    data["observability"] = {"jsonl_max_bytes": max_bytes, "jsonl_keep": keep}
    conf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_rotate_creates_numbered_files(tmp_path):
    """纯函数层：到上限就滚动，最旧的超份数被丢。"""
    path = tmp_path / "x.jsonl"
    path.write_text("a" * 200 + "\n", encoding="utf-8")
    actions = jsonl_log.rotate_if_needed(path, max_bytes=100, keep=2)
    assert actions and path.with_name("x.jsonl.1").exists()
    assert not path.exists() or path.stat().st_size == 0

    # 再滚两轮：keep=2 → 最多留 .1 与 .2
    path.write_text("b" * 200 + "\n", encoding="utf-8")
    jsonl_log.rotate_if_needed(path, max_bytes=100, keep=2)
    path.write_text("c" * 200 + "\n", encoding="utf-8")
    jsonl_log.rotate_if_needed(path, max_bytes=100, keep=2)
    assert path.with_name("x.jsonl.1").exists() and path.with_name("x.jsonl.2").exists()
    assert not path.with_name("x.jsonl.3").exists(), "超过保留份数的滚动文件应被丢掉"


def test_keep_zero_truncates(tmp_path):
    """keep=0：不保留历史，直接截断当前文件（磁盘告急时的极端档）。"""
    path = tmp_path / "y.jsonl"
    path.write_text("z" * 500 + "\n", encoding="utf-8")
    actions = jsonl_log.rotate_if_needed(path, max_bytes=100, keep=0)
    assert any("truncated" in a for a in actions)
    assert path.read_text(encoding="utf-8") == ""


def test_audit_rotates_and_keeps_latest(home):
    """审计文件：写满就滚动，最新一条仍在当前文件里（不难为空）。"""
    _cfg(home, max_bytes=64 * 1024, keep=2)  # 用最小允许上限
    account = "rot1"
    for i in range(400):
        audit.record_retrieval(
            account,
            query=f"查询{i}",
            candidates=[{"doc_id": f"m{i}", "kind": "memory", "channel": "semantic", "score": 0.9}],
            injected_ids=[f"m{i}"],
        )
    live = audit.audit_path(account)
    assert live.exists()
    assert live.with_name(live.name + ".1").exists(), "写满上限后应产生滚动文件"
    events = audit.load_events(live)
    assert events, "当前文件必须有内容（最新事件不该丢）"
    assert any("查询399" in e.get("query", "") for e in events[-5:]), "最后写入的事件应在当前文件尾部"
    merged = audit.load_events(live, include_rotated=True)
    assert len(merged) > len(events), "合并滚动份后应读到更多历史"


def test_observe_rotates(home):
    """观察文件：同一套轮转（注入/确认两种事件都走它）。"""
    _cfg(home, max_bytes=64 * 1024, keep=1)
    account = "rot2"
    long_query = "观测轮转测试" * 30  # 单条事件够大：400 条才会越过最小允许上限（64 KiB）
    for i in range(400):
        observe_log.log_injection(account, f"{long_query}-{i}", [f"m{i}"])
    live = observe_log.observe_path(account)
    assert live.exists() and live.with_name(live.name + ".1").exists()
    events = jsonl_log.load_events(live)
    assert any("问题" not in e.get("query", "") and str(399) in e.get("query", "") for e in events[-5:])


def test_limits_from_config(home, monkeypatch):
    """上限/份数确实来自配置（env 覆盖也认）。"""
    _cfg(home, max_bytes=128 * 1024, keep=5)
    monkeypatch.delenv("HIPPOCAMPUS_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("HIPPOCAMPUS_LOG_KEEP", raising=False)
    assert jsonl_log.log_limits() == (128 * 1024, 5)
    monkeypatch.setenv("HIPPOCAMPUS_LOG_KEEP", "7")
    assert jsonl_log.log_limits()[1] == 7


def test_bad_limits_fall_back(tmp_path):
    """非法上限值退回默认区间（配置手改出错不该让日志写崩）。"""
    path = tmp_path / "bad.jsonl"
    path.write_text("x\n", encoding="utf-8")
    max_bytes, keep = jsonl_log.log_limits()
    assert max_bytes >= jsonl_log.MIN_MAX_BYTES
    assert 0 <= keep <= jsonl_log.MAX_KEEP


def test_append_soft_fails_on_unwritable_path(tmp_path):
    """写不进去（父路径是文件）→ 只记 stderr，不抛（日志坏掉不能拖垮注入链）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    jsonl_log.append_jsonl(blocker / "sub" / "x.jsonl", {"event": "x"})  # 不抛即通过


def test_audit_events_still_parse(home):
    """轮转后当前文件仍是合法 JSONL（explain 读它，格式不能坏）。"""
    _cfg(home, max_bytes=64 * 1024, keep=1)
    account = "rot3"
    for i in range(50):
        audit.record_retrieval(account, query=f"q{i}", candidates=[], injected_ids=[])
    for event in audit.load_events(audit.audit_path(account)):
        assert event["event"] == "audit.retrieval"
        assert "ts" in event and "query" in event
