"""最小结构化 trace：OTel 数据模型 ＋ OTLP/JSON 文件导出（纯标准库）。

## 定位（读代码前先看这里）
本模块**不是** OTel SDK 的替代品，只补本仓缺的一件东西：**一次检索能落一条带层级的
结构化 trace**（此前只有 `audit.jsonl` / `observe.jsonl` 两类扁平事件，无 span 层级、
无延迟分解）。字段名按 OTLP/JSON 的 proto3 JSON 映射写，所以文件可以直接喂给
Jaeger/Tempo/Collector 一类工具，也可以当普通 JSON 读。

## 数据模型（与 OTLP 对齐）
- 一条 trace = 同一 `traceId` 下的 span 树；span 自带 `parentSpanId`（无父为空串）。
- 时间统一用 **Unix 纳秒字符串**（OTLP 规定 uint64 走字符串），`time.time_ns()` 直出。
- 属性值走 `{"stringValue": ...}` / `{"intValue": "..."}` / `{"doubleValue": ...}` /
  `{"boolValue": ...}` 四态，与 OTLP `AnyValue` 一致；`intValue` 按 proto3 JSON 规定写成字符串。
- `status.code`：0＝UNSET、1＝OK、2＝ERROR（OTLP `StatusCode` 原值）。

## 并发
检索链会被 `ThreadPoolExecutor` 并发调用（`bench_ab` / `model_arm` 的批跑路径），
因此 span 栈与缓冲都用 `threading.local` + 锁：**父 span 只在同一线程内解析**，
不跨线程错挂。这与 OTel 的 context 语义差异写在 `docs/observability.md`（同线程树＝
调用树，跨线程不串联）——是刻意的简化，不是遗漏。

## 开销
未 `start_export()` 且无 `HIPPOCAMPUS_TRACE_FILE` 时，`get_tracer()` 返回的 tracer
所有方法立即返回共享的 `_NULL_SPAN`，不取时间、不分配列表、不落盘。
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# OTLP StatusCode：0 UNSET / 1 OK / 2 ERROR
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

ENV_TRACE_FILE = "HIPPOCAMPUS_TRACE_FILE"
SERVICE_NAME = "hippocampus"


def _new_id(nbytes: int) -> str:
    """traceId=16B／spanId=8B 的十六进制串（OTLP 规定的小写 hex 长度）。"""
    return secrets.token_hex(nbytes)


def _any_value(value: Any) -> dict[str, Any] | None:
    """Python 值 → OTLP AnyValue。不认识的类型转字符串（永不抛，观测不拖业务）。"""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}  # proto3 JSON：int64 走字符串
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if value is None:
        return None
    try:
        return {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}
    except (TypeError, ValueError):
        return {"stringValue": str(value)}


def _attrs(items: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, value in items.items():
        encoded = _any_value(value)
        if encoded is not None:
            out.append({"key": key, "value": encoded})
    return out


class Span:
    """一个已完成／进行中的 span。字段名对齐 OTLP/JSON 的 `Span` 消息。"""

    __slots__ = (
        "attributes",
        "ended_ns",
        "kind",
        "name",
        "parent_span_id",
        "span_id",
        "started_ns",
        "status_code",
        "status_message",
        "trace_id",
    )

    def __init__(self, name: str, *, trace_id: str, span_id: str, parent_span_id: str, kind: str) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.kind = kind  # OTLP SpanKind 名（INTERNAL／CLIENT…），本模块只用前两者
        self.started_ns = time.time_ns()
        self.ended_ns = 0
        self.attributes: dict[str, Any] = {}
        self.status_code = STATUS_UNSET
        self.status_message = ""

    # ---- 写入面（全部立即生效；span 未导出时业务侧不用关心）----
    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_attributes(self, **items: Any) -> None:
        self.attributes.update(items)

    def set_status(self, code: int, message: str = "") -> None:
        self.status_code = code
        self.status_message = message

    def end(self) -> None:
        if not self.ended_ns:
            self.ended_ns = time.time_ns()

    # ---- 读出面 ----
    @property
    def duration_ms(self) -> float:
        end = self.ended_ns or time.time_ns()
        return (end - self.started_ns) / 1e6

    def to_otlp(self) -> dict[str, Any]:
        """OTLP/JSON 的 `Span` 表示（唯一序列化出口，测试与导出器共用）。"""
        status: dict[str, Any] = {"code": self.status_code}
        if self.status_message:
            status["message"] = self.status_message
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.started_ns),
            "endTimeUnixNano": str(self.ended_ns or time.time_ns()),
            "attributes": _attrs(self.attributes),
            "status": status,
        }


class _NullSpan(Span):
    """未开启导出时的共享空 span：记录动作全部丢弃，语义上永不报错。"""

    def __init__(self) -> None:
        super().__init__("", trace_id="", span_id="", parent_span_id="", kind="INTERNAL")

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102 - 空实现
        return None

    def set_attributes(self, **items: Any) -> None:  # noqa: D102 - 空实现
        return None

    def set_status(self, code: int, message: str = "") -> None:  # noqa: D102 - 空实现
        return None

    def end(self) -> None:  # noqa: D102 - 空实现
        return None


_NULL_SPAN = _NullSpan()


class _Exporter:
    """收集已结束的 span，按需落一份 OTLP/JSON。线程安全、可重入。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spans: list[Span] = []
        self.path: Path | None = None
        self.enabled = False
        self.flushes = 0

    def enable(self, path: str | os.PathLike[str] | None, *, reset: bool = True) -> None:
        """开启导出并把路径设为 `path`。`reset=True` 清空缓冲（开一段新的导出＝新文件）。

        `reset=False` 用于"落盘到别的路径但保留已收集的 span"（`stop_export(path=...)`）。
        """
        with self.lock:
            self.enabled = True
            if reset:
                self.spans = []
            if path is not None:
                self.path = Path(path)

    def disable(self) -> None:
        with self.lock:
            self.enabled = False

    def emit(self, span: Span) -> None:
        with self.lock:
            if self.enabled:
                self.spans.append(span)

    def write(self) -> Path | None:
        """把**已收集的全部** span 写成 OTLP/JSON（累积语义，不是"只写增量"）。

        累积是刻意的：文件导出器重复 `flush()` 必须得到同一份完整 trace，
        否则第二次落盘会把第一次的内容缩成残片（本模块初版就踩了这个坑）。
        未配置路径则只累计、不落盘（返回 None）。
        """
        with self.lock:
            spans, path = list(self.spans), self.path
            self.flushes += 1
        if path is None:
            return None
        payload = otlp_payload(spans)
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 无 BOM；ensure_ascii=False 保住中文属性可读
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


_EXPORTER = _Exporter()

# span 栈按线程隔离：父 span 只在同一线程内解析（见模块 docstring「并发」）
_LOCAL = threading.local()


def _stack() -> list[Span]:
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _LOCAL.stack = stack
    return stack


class Tracer:
    """span 工厂。唯一实例由 `get_tracer()` 返回，业务侧不要自己 new。"""

    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self.service_name = service_name

    @contextlib.contextmanager
    def span(self, name: str, *, kind: str = "INTERNAL", **attributes: Any) -> Iterator[Span]:
        """开一个 span 并作为当前线程的父；`with` 块内再开 span 自动挂成子。

        异常路径：记 `status=ERROR` 与原样消息后**继续抛**（观测不改控制流）。
        """
        if not _EXPORTER.enabled:
            yield _NULL_SPAN
            return
        stack = _stack()
        parent = stack[-1] if stack else None
        span = Span(
            name,
            trace_id=parent.trace_id if parent else _new_id(16),
            span_id=_new_id(8),
            parent_span_id=parent.span_id if parent else "",
            kind=kind,
        )
        span.attributes.update(attributes)
        stack.append(span)
        try:
            yield span
        except BaseException as e:  # 记账后原样抛出，绝不吞
            span.set_status(STATUS_ERROR, f"{type(e).__name__}: {e}"[:200])
            raise
        finally:
            if stack and stack[-1] is span:
                stack.pop()
            elif span in stack:  # 异常展开顺序兜底
                stack.remove(span)
            span.end()
            _EXPORTER.emit(span)

    def start_span(self, name: str, *, kind: str = "INTERNAL", **attributes: Any) -> Span:
        """不开 `with` 的手动形态（多返回点函数用得上）：调用方负责 `end()` 与 `emit()`。"""
        if not _EXPORTER.enabled:
            return _NULL_SPAN
        stack = _stack()
        parent = stack[-1] if stack else None
        span = Span(
            name,
            trace_id=parent.trace_id if parent else _new_id(16),
            span_id=_new_id(8),
            parent_span_id=parent.span_id if parent else "",
            kind=kind,
        )
        span.attributes.update(attributes)
        return span

    def emit(self, span: Span) -> None:
        """把 `start_span()` 造出来的 span 收进导出缓冲（`_NULL_SPAN` 自动忽略）。"""
        if span is _NULL_SPAN:
            return
        span.end()
        _EXPORTER.emit(span)


_TRACER = Tracer()


def get_tracer() -> Tracer:
    """唯一 tracer 入口。首次调用时会看 `HIPPOCAMPUS_TRACE_FILE`（存在即自动开导出）。"""
    if not _EXPORTER.enabled:
        env_path = os.environ.get(ENV_TRACE_FILE)
        if env_path:
            _EXPORTER.enable(env_path)
    return _TRACER


def start_export(path: str | os.PathLike[str] | None = None) -> None:
    """开启导出。`path=None` 时只缓冲不落盘（给测试与内存分析用）。"""
    _EXPORTER.enable(path)


def flush() -> str | None:
    """立刻落盘一次，返回写入路径（未配路径返回 None）。

    累积语义：文件里始终是**已收集的全部 span**，重复调用不会把内容缩成增量残片。
    """
    written = _EXPORTER.write()
    return str(written) if written is not None else None


def stop_export(path: str | os.PathLike[str] | None = None) -> str | None:
    """落盘并关闭导出。`path` 给了就改存到该路径（不清空已收集的 span，仍是一份完整 trace）。"""
    if path is not None:
        _EXPORTER.enable(path, reset=False)
    written = _EXPORTER.write()
    _EXPORTER.disable()
    return str(written) if written is not None else None


def current_trace_file() -> str | None:
    """当前导出路径（未配置返回 None）——给脚本回显与测试断言用。"""
    return str(_EXPORTER.path) if _EXPORTER.path is not None else None


def otlp_payload(spans: list[Span]) -> dict[str, Any]:
    """span 列表 → OTLP/JSON `ExportTraceServiceRequest` 形状（resourceSpans 单元素）。"""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attrs(
                        {"service.name": SERVICE_NAME, "telemetry.sdk.name": "hippocampus-observability"}
                    )
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "hippocampus.observability", "version": "1"},
                        "spans": [s.to_otlp() for s in spans if s is not _NULL_SPAN],
                    }
                ],
            }
        ]
    }
