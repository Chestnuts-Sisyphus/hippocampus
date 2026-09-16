"""T3 交付物：`MemoryCore` v1 接口契约的**自动检查**（CI 门禁）。

检查三条冻结约定（docs/memory-core-v1.md）：
1. 公开方法第一参数是 scope（生命周期/诊断方法在豁免表里）；
2. 公开方法与公开类型里**不出现 HTTP 概念**（request／body／header／messages／
   status_code／endpoint 等词）——记忆语义与传输协议无关；
3. v1 只追加：数据的必填字段集合必须等于冻结基线（新增字段必须带默认值）。

退出码 0 = 契约成立。跑法：`python scripts/check_interface.py`
"""

from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hippocampus.core import MemoryCore  # noqa: E402
from hippocampus.core import types as core_types  # noqa: E402

HTTP_WORDS = (
    "request",
    "response",
    "http",
    "header",
    "body",
    "cookie",
    "status_code",
    "endpoint",
    "messages",
    "payload",
    "json_body",
    "url_path",
)

SCOPE_EXEMPT = {"close", "embedding_tier", "lock_status", "unlock"}

# v1 冻结基线：这些字段在 v1 就是必填，之后的**任何新增字段都必须带默认值**
FROZEN_REQUIRED_FIELDS = {
    "MemoryItem": {"id", "kind", "content"},
    "Dropped": {"id", "reason"},
}


def check_scope_first() -> list[str]:
    bad = []
    for name, member in inspect.getmembers(MemoryCore, predicate=inspect.isfunction):
        if name.startswith("_") or name in SCOPE_EXEMPT:
            continue
        params = [p for p in inspect.signature(member).parameters if p != "self"]
        if not params or params[0] != "scope":
            bad.append(f"{name}{inspect.signature(member)}")
    return bad


def check_no_http_vocabulary() -> list[str]:
    bad = []
    for name, member in inspect.getmembers(MemoryCore, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        blob = f"{name}{inspect.signature(member)}".lower()
        for word in HTTP_WORDS:
            if word in blob:
                bad.append(f"方法 {name}: 出现 HTTP 词汇 {word!r}")
    for type_name in dir(core_types):
        obj = getattr(core_types, type_name)
        if not dataclasses.is_dataclass(obj):
            continue
        for field in dataclasses.fields(obj):
            for word in HTTP_WORDS:
                if word in field.name.lower():
                    bad.append(f"类型 {type_name}.{field.name}: 出现 HTTP 词汇 {word!r}")
    return bad


def check_append_only() -> list[str]:
    bad = []
    for type_name, expected in FROZEN_REQUIRED_FIELDS.items():
        obj = getattr(core_types, type_name)
        required = {
            f.name
            for f in dataclasses.fields(obj)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        if required != expected:
            bad.append(f"{type_name} 的必填字段集合变了：{sorted(required)} != {sorted(expected)}")
    return bad


def main() -> int:
    groups = {
        "① scope 为第一参数": check_scope_first(),
        "② 无 HTTP 词汇": check_no_http_vocabulary(),
        "③ v1 只追加（必填字段基线不变）": check_append_only(),
    }
    ok = True
    for title, problems in groups.items():
        if problems:
            ok = False
            print(f"✗ {title}")
            for problem in problems:
                print(f"    - {problem}")
        else:
            print(f"✓ {title}")
    print()
    print("MemoryCore v1 接口契约：" + ("成立" if ok else "被破坏"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
