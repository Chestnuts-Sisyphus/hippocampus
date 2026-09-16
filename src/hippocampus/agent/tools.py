"""Agent 形态的工具面（工具纪律四件）。

工具纪律（方案 §16.3-2）：
1. **参数调用前校验**：每个工具自带 JSON Schema 子集校验器，校验不过不进函数体；
2. **错误四分类回灌**：参数错 / 被拒 / 执行错 / 环境错——分类回灌给编排层，让它换路或收口；
3. **白名单与危险确认**：写文件、出站抓取属危险动作，必须经确认闸（人在环）；
4. **健康视图**：连续失败计数 + 熔断标记（只读视图，供编排层换路）。

出站抓取走 `hippocampus.net.validate_outbound_url`（验收 A41：仅 http/https，
拒环回／私有／保留）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hippocampus.core import MemoryCore, Scope
from hippocampus.net import UnsafeURLError, validate_outbound_url

# ----------------------------------------------------------------------
# 错误分类
# ----------------------------------------------------------------------

PARAM_ERROR = "参数错"
DENIED = "被拒"
TOOL_ERROR = "工具错"
ENV_ERROR = "环境错"


class ToolErrorBase(Exception):
    category = TOOL_ERROR


class ToolParamError(ToolErrorBase):
    category = PARAM_ERROR


class ToolDenied(ToolErrorBase):
    category = DENIED


class ToolEnvError(ToolErrorBase):
    category = ENV_ERROR


# ----------------------------------------------------------------------
# 参数校验（调用前；JSON Schema 子集：type/required/enum/min/max）
# ----------------------------------------------------------------------


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> None:
    """调用前校验；不通过抛 ToolParamError（附字段与期望类型）。"""
    if not isinstance(args, dict):
        raise ToolParamError(f"参数必须是对象，收到 {type(args).__name__}")
    props: dict[str, Any] = schema.get("properties") or {}
    for name in schema.get("required", []):
        if name not in args or args[name] in (None, ""):
            raise ToolParamError(f"缺少必填参数 {name!r}")
    for name, value in args.items():
        spec = props.get(name)
        if spec is None:
            if schema.get("additionalProperties", True) is False:
                raise ToolParamError(f"不接受未知参数 {name!r}")
            continue
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolParamError(f"参数 {name!r} 应为字符串，收到 {type(value).__name__}")
        if expected == "integer" and not isinstance(value, int):
            raise ToolParamError(f"参数 {name!r} 应为整数，收到 {type(value).__name__}")
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolParamError(f"参数 {name!r} 应为布尔，收到 {type(value).__name__}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolParamError(f"参数 {name!r} 必须是 {spec['enum']} 之一，收到 {value!r}")
        if isinstance(value, (int, float)) and "min" in spec and value < spec["min"]:
            raise ToolParamError(f"参数 {name!r} 小于最小值 {spec['min']}")
        if isinstance(value, str) and "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ToolParamError(f"参数 {name!r} 超过长度上限 {spec['maxLength']}")


# ----------------------------------------------------------------------
# 工具定义
# ----------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False        # 需要人在环确认
    whitelisted: bool = True       # 白名单（False = 一律拒绝）

    def as_langchain(self) -> dict[str, Any]:
        """给 LangChain／模型看的描述（OpenAI function 形态）。"""
        return {"name": self.name, "description": self.description, "parameters": self.schema}


@dataclass
class ToolHealth:
    """健康视图：连续失败计数 + 熔断标记（只读，供编排层换路）。"""

    failures: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    threshold: int = 3

    def record(self, name: str, error: str) -> None:
        self.failures[name] = self.failures.get(name, 0) + 1
        self.last_error[name] = error

    def record_success(self, name: str) -> None:
        self.failures[name] = 0
        self.last_error.pop(name, None)

    def is_open(self, name: str) -> bool:
        """熔断已打开（连续失败达阈值）→ 编排层应换路。"""
        return self.failures.get(name, 0) >= self.threshold


class ToolBox:
    """一组工具 + 健康视图 + 危险动作确认闸。"""

    def __init__(
        self,
        core: MemoryCore,
        scope: Scope,
        *,
        workdir: Path | None = None,
        allow_write: bool = False,
        confirm: Callable[[str, dict[str, Any]], bool] | None = None,
        url_fetcher: Callable[[str], str] | None = None,
    ) -> None:
        self.core = core
        self.scope = scope
        self.workdir = Path(workdir) if workdir else Path.cwd()
        self.allow_write = allow_write
        self.confirm = confirm
        self.url_fetcher = url_fetcher
        self.health = ToolHealth()
        self.call_log: list[dict[str, Any]] = []
        self.tools: dict[str, ToolSpec] = {t.name: t for t in self._build()}

    # ---------- 执行 ----------

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """唯一调用入口：校验 → 危险确认 → 执行 → 分类回灌 → 健康记账。"""
        spec = self.tools.get(name)
        if spec is None:
            return self._fail(name, PARAM_ERROR, f"未知工具 {name!r}（可用：{sorted(self.tools)}）")
        if not spec.whitelisted:
            return self._fail(name, DENIED, f"工具 {name} 不在白名单")
        try:
            validate_args(spec.schema, args)
        except ToolParamError as e:
            return self._fail(name, PARAM_ERROR, str(e))
        if spec.dangerous and not self.allow_write:
            if self.confirm is None or not self.confirm(name, args):
                return self._fail(name, DENIED, f"危险动作需要确认：{name}")
        try:
            result = spec.func(**args)
            self.health.record_success(name)
            self.call_log.append({"tool": name, "args": args, "ok": True, "ts": int(time.time() * 1000)})
            return {"ok": True, "tool": name, "result": result}
        except ToolErrorBase as e:
            return self._fail(name, e.category, str(e))
        except Exception as e:  # noqa: BLE001 —— 工具内部异常统一归类为环境错
            return self._fail(name, ENV_ERROR, f"{type(e).__name__}: {e}")

    def _fail(self, name: str, category: str, message: str) -> dict[str, Any]:
        self.health.record(name, message)
        self.call_log.append(
            {"tool": name, "ok": False, "category": category, "error": message, "ts": int(time.time() * 1000)}
        )
        return {"ok": False, "tool": name, "category": category, "error": message}

    # ---------- 工具实现 ----------

    def _build(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="search_memory",
                description="检索记忆：返回与查询相关的记忆条目（含来源与命中通道）",
                schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 500},
                        "limit": {"type": "integer", "min": 1},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                func=self._search_memory,
            ),
            ToolSpec(
                name="remember",
                description="写入一条记忆（来源=用户/本轮会话），返回记忆 id",
                schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "maxLength": 1000},
                        "kind": {"type": "string", "enum": ["preference", "fact", "resource", "status"]},
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
                func=self._remember,
            ),
            ToolSpec(
                name="list_memories",
                description="列出当前生效的记忆（可按类型过滤）",
                schema={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["preference", "fact", "resource", "status"]},
                        "limit": {"type": "integer", "min": 1},
                    },
                    "additionalProperties": False,
                },
                func=self._list_memories,
            ),
            ToolSpec(
                name="write_file",
                description="把内容写入工作目录下的文件（危险动作，需确认）",
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "maxLength": 200},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                func=self._write_file,
                dangerous=True,
            ),
            ToolSpec(
                name="fetch_url",
                description="抓取一个 http/https 页面文本（危险动作，需确认；内网地址一律拒绝）",
                schema={
                    "type": "object",
                    "properties": {"url": {"type": "string", "maxLength": 500}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
                func=self._fetch_url,
                dangerous=True,
            ),
        ]

    def _search_memory(self, query: str, limit: int = 5) -> dict[str, Any]:
        result = self.core.search(self.scope, query, limit=limit)
        return {
            "items": [
                {"id": i.id, "kind": i.kind, "content": i.content, "score": round(i.score, 4), "channel": i.channel}
                for i in result.items
            ],
            "channels": {k: v for k, v in result.channels.items() if isinstance(v, (int, float))},
        }

    def _remember(self, content: str, kind: str = "preference") -> dict[str, Any]:
        result = self.core.write(self.scope, content, kind=kind, source_quote="agent 工具写入", explicit=True)
        return {
            "ids": result.ids,
            "superseded": result.superseded,
            "pending": result.pending,
            "skipped": [d.reason for d in result.skipped],
        }

    def _list_memories(self, kind: str | None = None, limit: int = 20) -> dict[str, Any]:
        items = self.core.list_memories(self.scope, limit=limit, kind=kind, status="active")
        return {"items": [{"id": i.id, "kind": i.kind, "content": i.content} for i in items]}

    def _write_file(self, path: str, content: str) -> dict[str, Any]:
        target = (self.workdir / path).resolve()
        root = self.workdir.resolve()
        if root not in target.parents and target != root:
            raise ToolParamError(f"目标路径越出工作目录: {path}（只允许写 {root} 下）")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target), "bytes": len(content.encode("utf-8"))}

    def _fetch_url(self, url: str) -> dict[str, Any]:
        try:
            safe = validate_outbound_url(url)
        except UnsafeURLError as e:
            raise ToolDenied(str(e)) from e
        if self.url_fetcher is None:
            raise ToolEnvError("未配置抓取器（离线档不提供出站抓取）")
        text = self.url_fetcher(safe)
        return {"url": safe, "chars": len(text), "text": text[:2000]}

    # ---------- 描述 ----------

    def describe(self) -> list[dict[str, Any]]:
        return [spec.as_langchain() for spec in self.tools.values()]

    def health_view(self) -> dict[str, Any]:
        return {
            "failures": dict(self.health.failures),
            "open": [n for n in self.tools if self.health.is_open(n)],
            "last_error": dict(self.health.last_error),
        }


__all__ = [
    "DENIED",
    "ENV_ERROR",
    "PARAM_ERROR",
    "TOOL_ERROR",
    "ToolBox",
    "ToolDenied",
    "ToolEnvError",
    "ToolHealth",
    "ToolParamError",
    "ToolSpec",
    "validate_args",
]
