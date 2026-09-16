"""Agent 形态：LangGraph 编排 + LangChain 工具接入 + 轨迹。

与记忆层的关系：本包只经 `hippocampus.core.MemoryCore` 访问记忆（不 import
`hippocampus.memory` 的内部模块）——这是"两种形态共用同一个记忆核心"的落地约束。
"""

from hippocampus.agent.runner import ReplayResult, RunResult, replay, run_task
from hippocampus.agent.tools import ToolBox

__all__ = ["ReplayResult", "RunResult", "ToolBox", "replay", "run_task"]
