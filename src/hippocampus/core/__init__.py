"""记忆核心的对外门面。

形态层（`hippocampus.proxy` / `hippocampus.agent`）只依赖本包，不直接 import
`hippocampus.memory` 的内部模块——这是"两个形态共用同一个记忆核心"的落地方式。
"""

from hippocampus.core.core import MemoryCore, MemoryCoreError
from hippocampus.core.locks import WriterBusy, WriterLock
from hippocampus.core.types import (
    ConfirmResult,
    Dropped,
    Injection,
    MemoryItem,
    MemoryKind,
    Scope,
    SearchResult,
    Source,
    TurnResult,
    WriteResult,
)

__all__ = [
    "ConfirmResult",
    "Dropped",
    "Injection",
    "MemoryCore",
    "MemoryCoreError",
    "MemoryItem",
    "MemoryKind",
    "Scope",
    "SearchResult",
    "Source",
    "TurnResult",
    "WriteResult",
    "WriterBusy",
    "WriterLock",
]
