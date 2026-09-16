"""验收 T3：`MemoryCore` v1 接口契约。

三条契约（docs/memory-core-v1.md）：
1. 公开方法**第一参数是 scope**；
2. 公开签名与类型里**不出现 HTTP 概念**（记忆语义与传输协议无关）；
3. v1 **只允许追加字段**（新字段必须有默认值）。

本文件提供契约的可执行版本；`scripts/check_interface.py` 做同样的检查并可在 CI 里
对"没有测试跑到的接口"兜底。
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.core import types as core_types

HTTP_WORDS = (
    "request",
    "response",
    "http",
    "header",
    "body",
    "cookie",
    "status_code",
    "endpoint",
    "url_path",
    "messages",
    "payload",
    "json_body",
)


def _public_methods() -> dict[str, inspect.Signature]:
    out = {}
    for name, member in inspect.getmembers(MemoryCore, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        out[name] = inspect.signature(member)
    return out


# 生命周期/诊断类方法不涉及"哪份记忆"，因此不受"scope 第一参数"约束
SCOPE_EXEMPT = {"close", "embedding_tier", "lock_status", "unlock"}


def test_scope_is_first_parameter():
    """除生命周期/诊断外，每个公开方法的第一参数都是 scope。"""
    offenders = []
    for name, sig in _public_methods().items():
        if name in SCOPE_EXEMPT:
            continue
        params = [p for p in sig.parameters if p != "self"]  # 类上取签名，首参是 self
        if not params or params[0] != "scope":
            offenders.append(f"{name}{sig}")
    assert not offenders, f"这些公开方法的第一参数不是 scope: {offenders}"


def test_no_http_vocabulary_in_signatures():
    """公开方法签名里不出现 HTTP 词汇。"""
    offenders = []
    for name, sig in _public_methods().items():
        blob = f"{name}{sig}".lower()
        for word in HTTP_WORDS:
            if word in blob:
                offenders.append(f"{name}: {word}")
    assert not offenders, f"接口签名里出现 HTTP 词汇: {offenders}"


def test_no_http_vocabulary_in_types():
    """公开类型（dataclass 字段）里不出现 HTTP 词汇。"""
    offenders = []
    for name in dir(core_types):
        obj = getattr(core_types, name)
        if not dataclasses.is_dataclass(obj):
            continue
        for field in dataclasses.fields(obj):
            if any(word in field.name.lower() for word in HTTP_WORDS):
                offenders.append(f"{name}.{field.name}")
    assert not offenders, f"公开类型里出现 HTTP 词汇: {offenders}"


# v1 冻结时"必填字段"的基线。**新增字段必须带默认值**，否则这个集合会变、测试会红
# ——这就是"只允许追加"的可执行版本。
FROZEN_REQUIRED_FIELDS = {
    "MemoryItem": {"id", "kind", "content"},
    "Dropped": {"id", "reason"},
}


def test_new_dataclass_fields_have_defaults():
    """除冻结基线里的必填字段外，其余字段都必须有默认值。"""
    offenders = []
    for name in dir(core_types):
        obj = getattr(core_types, name)
        if not dataclasses.is_dataclass(obj):
            continue
        allowed = FROZEN_REQUIRED_FIELDS.get(name, set())
        for field in dataclasses.fields(obj):
            has_default = field.default is not dataclasses.MISSING or field.default_factory is not dataclasses.MISSING
            if not has_default and field.name not in allowed:
                offenders.append(f"{name}.{field.name}")
    assert not offenders, f"这些新增字段没有默认值（v1 只允许追加且必须带默认值）：{offenders}"


def test_frozen_required_fields_unchanged():
    """冻结基线的必填字段集合本身不许变（改名/删字段都算破坏 v1）。"""
    for name, expected in FROZEN_REQUIRED_FIELDS.items():
        obj = getattr(core_types, name)
        required = {
            f.name
            for f in dataclasses.fields(obj)
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        }
        assert required == expected, f"{name} 的必填字段集合变了：{required} != {expected}"


def test_dataclass_is_frozen_where_expected():
    """Scope 冻结（值语义），可变容器类类型不冻结（要能 append）。"""
    assert Scope.__dataclass_params__.frozen is True


def test_methods_documented():
    """公开方法必须有 docstring（冻结接口的另一半是可读性）。"""
    missing = [name for name, _ in _public_methods().items() if not (getattr(MemoryCore, name).__doc__ or "").strip()]
    assert not missing, f"这些公开方法没有 docstring: {missing}"


def test_core_requires_explicit_scope(core):
    """scope 必须显式传（防"忘了 scope 用默认库"这类事故）。"""
    from hippocampus.core import MemoryCoreError

    with pytest.raises(MemoryCoreError):
        core.write(None, "内容")  # type: ignore[arg-type]


def test_write_search_inject_confirm_exist():
    """冻结的五个入口都在。"""
    for name in ("write", "search", "inject_finalize", "confirm", "consolidate"):
        assert hasattr(MemoryCore, name), f"缺少冻结接口 {name}"
