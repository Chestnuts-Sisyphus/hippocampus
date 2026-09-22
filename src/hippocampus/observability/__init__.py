"""可观测性（结构化 trace）——纯标准库实现，零新依赖。

为什么不用 `opentelemetry-sdk`：本仓 `pyproject.toml` 没有声明它，它只随
**可选档 `vector`（chromadb）** 一起进环境；核心档与 CI 的 core-only job 装不上。
把它引成硬依赖＝十轮 X15 点名的「未声明／漂移依赖」同类问题。因此这里按
**OTel 数据模型**自实现最小记录器，导出 **OTLP/JSON**（标准工具可直接吃），
在未开启导出时全程 no-op（对 649 条测试零行为变更）。

对外只有三件：
- `get_tracer()`：拿 tracer（无参、线程安全、无副作用）；
- `tracer.span(name, **attrs)`：上下文管理器，产出一个 span；
- `start_export(path)` / `flush()` / `stop_export()`：把已结束的 span 写成 OTLP/JSON 文件。

开关：显式 `start_export()`，或环境变量 `HIPPOCAMPUS_TRACE_FILE` 指向落盘路径。
"""

from hippocampus.observability.tracing import (
    ENV_TRACE_FILE,
    SERVICE_NAME,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSET,
    Span,
    Tracer,
    current_trace_file,
    flush,
    get_tracer,
    otlp_payload,
    start_export,
    stop_export,
)

__all__ = [
    "ENV_TRACE_FILE",
    "SERVICE_NAME",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_UNSET",
    "Span",
    "Tracer",
    "current_trace_file",
    "flush",
    "get_tracer",
    "otlp_payload",
    "start_export",
    "stop_export",
]
