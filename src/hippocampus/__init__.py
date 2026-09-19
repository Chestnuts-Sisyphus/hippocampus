"""Hippocampus —— 以记忆为核心的 agent 运行时。

一个记忆核心（`MemoryCore`），两种消费形态：代理形态（OpenAI 兼容端点）与
Agent 形态（LangGraph 编排）。对外可插拔只限三点：记忆后端、工具来源、模型端点。
"""

from importlib import metadata as _metadata

from hippocampus.core import MemoryCore, Scope

try:  # 单一事实源＝已安装分发的元数据（`pyproject.toml` 的 version）
    __version__ = _metadata.version("hippocampus-agent")
except _metadata.PackageNotFoundError:  # 源码树直跑、未安装：给哨兵值，不写回一个旧版本号
    __version__ = "0.0.0+source"

__all__ = ["MemoryCore", "Scope", "__version__"]
