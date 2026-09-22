"""GF1·① 结构化 trace 的行为闸：span 树、OTLP/JSON 形状、no-op 语义、埋点接线。

缺口的原样：仓里只有 `audit.jsonl`／`observe.jsonl` 两类**扁平事件**，没有 span 层级、
没有延迟分解，也没有一条 trace 能把"嵌入 → 向量查询 → 注入"串起来。本文件钉四件事：

1. **未开导出＝完全 no-op**（不取时间、不缓冲、不落盘）——这是"不打扰 649 条既有测试"的前提；
2. **开导出后**：父子关系正确、同 traceId、ID 长度合规、属性按 OTLP AnyValue 编码、异常记 ERROR 后原样抛；
3. **累积落盘**：重复 flush 得到的是同一份完整 trace（初版曾把第二次 flush 写成空文件，这里留回归）；
4. **埋点真接线**：真跑一次注入路径，`hippocampus.injection` 必有；装了 chromadb 时
   `hippocampus.embedding` 与 `hippocampus.vector_query` 也必须有（core-only 档语义通道本就关闭）。

跑法：`pytest tests/test_n54_r10_gf1_trace_spans.py`（纯本地，零出站，零凭据）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hippocampus import observability as obs

HEX32 = 32
HEX16 = 16
HAS_CHROMADB = importlib.util.find_spec("chromadb") is not None


@pytest.fixture()
def tracing(tmp_path):
    """干净起点：先关掉可能残留的导出，再开一个指向 tmp 的导出。"""
    obs.stop_export()
    path = tmp_path / "trace.json"
    obs.start_export(path)
    yield path
    obs.stop_export()


def _spans(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    spans: list[dict] = []
    for resource_spans in payload["resourceSpans"]:
        for scope_spans in resource_spans["scopeSpans"]:
            spans.extend(scope_spans["spans"])
    return spans


def _attr(span: dict, key: str):
    for item in span.get("attributes", []):
        if item["key"] == key:
            value = item["value"]
            for kind in ("stringValue", "boolValue", "doubleValue", "intValue"):
                if kind in value:
                    return value[kind]
    return None


# ----------------------------------------------------------------------
# 1. 未开导出 = no-op
# ----------------------------------------------------------------------


def test_no_op_when_not_exporting(tmp_path):
    """未开导出时：span 是共享空对象，不落盘、不留痕（对既有行为零影响）。"""
    obs.stop_export()
    tracer = obs.get_tracer()
    with tracer.span("should.not.appear") as span:
        span.set_attribute("k", "v")
        span.set_status(1, "ok")
    assert obs.flush() is None, "未配路径时 flush 不应写文件"
    assert not list(tmp_path.glob("*.json"))


def test_env_var_opens_export(tmp_path, monkeypatch):
    """环境变量通道：`HIPPOCAMPUS_TRACE_FILE` 一处即可开启（给 proxy 等入口用）。"""
    obs.stop_export()
    path = tmp_path / "from_env.json"
    monkeypatch.setenv(obs.ENV_TRACE_FILE, str(path))
    tracer = obs.get_tracer()
    with tracer.span("env.opened"):
        pass
    written = obs.flush()
    obs.stop_export()
    monkeypatch.delenv(obs.ENV_TRACE_FILE, raising=False)
    assert written is not None and Path(written) == path
    assert [s["name"] for s in _spans(path)] == ["env.opened"]


# ----------------------------------------------------------------------
# 2. span 树与 OTLP 形状
# ----------------------------------------------------------------------


def test_span_tree_ids_and_attributes(tracing):
    """父子关系、traceId 一致、ID 长度合规、属性按 AnyValue 编码。"""
    tracer = obs.get_tracer()
    with tracer.span("root", **{"a.string": "值", "a.int": 7, "a.float": 0.5, "a.bool": True}):
        with tracer.span("child", kind="CLIENT") as child:
            child.set_attribute("nested", "yes")
        with tracer.span("child2"):
            pass
    obs.flush()
    spans = {s["name"]: s for s in _spans(tracing)}
    assert set(spans) == {"root", "child", "child2"}
    assert len({s["traceId"] for s in spans.values()}) == 1
    assert len(spans["root"]["traceId"]) == HEX32
    assert all(len(s["spanId"]) == HEX16 for s in spans.values())
    assert spans["root"]["parentSpanId"] == ""
    assert spans["child"]["parentSpanId"] == spans["root"]["spanId"]
    assert spans["child2"]["parentSpanId"] == spans["root"]["spanId"]
    assert spans["child"]["kind"] == "CLIENT"
    assert _attr(spans["root"], "a.int") == "7"  # proto3 JSON：int64 走字符串
    assert _attr(spans["root"], "a.bool") is True
    assert _attr(spans["root"], "a.string") == "值"
    assert float(_attr(spans["root"], "a.float")) == pytest.approx(0.5)
    assert _attr(spans["child"], "nested") == "yes"


def test_timestamps_allow_latency_breakdown(tracing):
    """每个 span 的起止时间可比较、且子 span 落在父 span 时间窗内（延迟分解的前提）。"""
    tracer = obs.get_tracer()
    with tracer.span("root"):
        with tracer.span("child"):
            pass
    obs.flush()
    spans = {s["name"]: s for s in _spans(tracing)}
    root_start, root_end = int(spans["root"]["startTimeUnixNano"]), int(spans["root"]["endTimeUnixNano"])
    child_start, child_end = int(spans["child"]["startTimeUnixNano"]), int(spans["child"]["endTimeUnixNano"])
    assert root_start <= child_start <= child_end <= root_end


def test_exception_records_error_and_reraises(tracing):
    """异常路径：记 status=ERROR 与原消息，然后**原样抛出**（观测绝不吞异常）。"""
    tracer = obs.get_tracer()
    with pytest.raises(ValueError, match="boom"):
        with tracer.span("failing"):
            raise ValueError("boom")
    obs.flush()
    span = _spans(tracing)[0]
    assert span["status"]["code"] == obs.STATUS_ERROR
    assert "boom" in span["status"]["message"]
    assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])


def test_repeated_flush_is_cumulative(tracing):
    """回归：重复 flush 得到同一份完整 trace，而不是"只剩增量"的残片。

    初版导出器写一次就清空缓冲，`flush()` + `stop_export()` 连用会把好文件覆盖成空——
    trace 脚本第一次跑就是这么失败的（0 个 span）。这条把它钉住。
    """
    tracer = obs.get_tracer()
    with tracer.span("first"):
        pass
    obs.flush()
    first = _spans(tracing)
    with tracer.span("second"):
        pass
    obs.flush()
    second = _spans(tracing)
    assert [s["name"] for s in first] == ["first"]
    assert [s["name"] for s in second] == ["first", "second"]


def test_stop_export_keeps_collected_spans(tracing):
    """stop_export 改存路径时不得丢已收集的 span。"""
    tracer = obs.get_tracer()
    with tracer.span("kept"):
        pass
    other = tracing.parent / "moved.json"
    written = obs.stop_export(other)
    assert Path(written) == other
    assert [s["name"] for s in _spans(other)] == ["kept"]


# ----------------------------------------------------------------------
# 3. 埋点真接线（端到端）
# ----------------------------------------------------------------------


def test_injection_path_really_emits_spans(core, scope, tracing):
    """真跑一次注入：三类 span 与它们的属性都必须出现（不是"我以为接上了"）。"""
    core.write(scope, "我的期望城市是杭州", kind="fact", source_quote="用户原话")
    core.write(scope, "我不喜欢回答里堆大段代码", kind="preference", source_quote="用户原话")
    # 写入路径自己也会走检索（去重/冲突检测），那些 span 没有外层 span → 各自成一条 trace。
    # 这是 OTel 的正常语义（无父的 span 就是新 trace 的根），不是 bug；本测试要断言的是
    # "一次检索 = 一条 trace"，所以先把写入阶段的 span 清出缓冲（start_export 会重置）。
    obs.start_export(tracing)
    injection = core.inject_finalize(scope, "我期望的城市是哪里？")
    obs.flush()
    spans = _spans(tracing)
    names = [s["name"] for s in spans]
    assert "hippocampus.injection" in names, f"注入埋点没接上：{names}"
    # 根 span 由 inject_finalize 自己开 → 一次检索就是一条 trace，不依赖调用方给根
    assert "hippocampus.retrieval_request" in names, f"根 span 没开：{names}"
    assert len({s["traceId"] for s in spans}) == 1, "一次检索必须只落一条 trace"
    roots = [s for s in spans if not s["parentSpanId"]]
    assert [s["name"] for s in roots] == ["hippocampus.retrieval_request"]

    injection_span = next(s for s in spans if s["name"] == "hippocampus.injection")
    # 注入层 span 记的是**检索返回条数**（上层还会定序/限额/装载，可能再减）
    assert int(_attr(injection_span, "hippocampus.injection.n_retrieved")) >= len(injection.items)
    assert int(_attr(injection_span, "hippocampus.injection.fluid_chars")) == len(injection.fluid_text or "")
    assert int(_attr(injection_span, "hippocampus.injection.stable_chars")) == len(injection.stable_text or "")
    # 最终注入条数记在根 span（inject_finalize 的出口口径）
    root_span = next(s for s in spans if s["name"] == "hippocampus.retrieval_request")
    assert int(_attr(root_span, "hippocampus.injection.items")) == len(injection.items)
    assert _attr(root_span, "hippocampus.account") == scope.account
    assert _attr(root_span, "hippocampus.flow") == "user"

    if HAS_CHROMADB:
        assert "hippocampus.embedding" in names, f"嵌入埋点没接上：{names}"
        assert "hippocampus.vector_query" in names, f"向量查询埋点没接上：{names}"
        embedding = next(s for s in spans if s["name"] == "hippocampus.embedding")
        assert int(_attr(embedding, "hippocampus.embedding.dim")) > 0
        vector = next(s for s in spans if s["name"] == "hippocampus.vector_query")
        assert int(_attr(vector, "hippocampus.vector_query.n_returned")) >= 0
        # 检索 span 必须挂在注入 span 之下（同线程调用树）
        assert embedding["parentSpanId"] == injection_span["spanId"]


def test_child_spans_nest_under_the_root_supplied_by_the_caller(core, scope, tracing):
    """调用方再给一层根时，检索链整棵挂在外层根之下（代理/脚本可以自建外层事务）。"""
    core.write(scope, "我的期望城市是杭州", kind="fact", source_quote="用户原话")
    obs.start_export(tracing)  # 清掉写入阶段的 span（无父 span 各自成根，见上一个测试的注释）
    tracer = obs.get_tracer()
    with tracer.span("outer.transaction") as outer:
        core.inject_finalize(scope, "我期望的城市是哪里？")
    obs.flush()
    spans = _spans(tracing)
    by_id = {s["spanId"]: s for s in spans}
    roots = [s for s in spans if s["parentSpanId"] == ""]
    assert [s["name"] for s in roots] == ["outer.transaction"]
    retrieval_span = next(s for s in spans if s["name"] == "hippocampus.retrieval_request")
    assert retrieval_span["parentSpanId"] == outer.span_id
    injection_span = next(s for s in spans if s["name"] == "hippocampus.injection")
    assert by_id[injection_span["parentSpanId"]]["name"] == "hippocampus.retrieval_request"


def test_otlp_payload_has_resource_and_scope():
    """导出件的 OTLP 骨架：resource 带 service.name，scope 带名字（标准工具可直接吃）。"""
    tracer = obs.get_tracer()
    obs.start_export(None)
    try:
        with tracer.span("x"):
            pass
        payload = obs.otlp_payload([])
    finally:
        obs.stop_export()
    resource = payload["resourceSpans"][0]["resource"]
    assert ("service.name", "hippocampus") in [(a["key"], a["value"].get("stringValue")) for a in resource["attributes"]]
    assert payload["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] == "hippocampus.observability"
