"""Hippocampus —— 以记忆为核心的 agent 运行时。

一个记忆核心（`MemoryCore`），两种消费形态：代理形态（OpenAI 兼容端点）与
Agent 形态（LangGraph 编排）。对外可插拔只限三点：记忆后端、工具来源、模型端点。
"""

from hippocampus.core import MemoryCore, Scope

__version__ = "0.2.0"

__all__ = ["MemoryCore", "Scope", "__version__"]
