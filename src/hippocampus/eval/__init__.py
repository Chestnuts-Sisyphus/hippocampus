"""评测（最小版：10 题 + 标签双来源 + 记忆开/关对照）。

诚实边界（必须随结果一起打印，见 runner.BOUNDARY）：
- 题量小（10 题），只够做**冒烟级**对照，不足以支撑效果结论；
- 离线档的作答器是**规则作答器**（把注入到的记忆按模板组织成回答），不是模型推理；
  因此离线档测的是"记忆层是否把该给的给出来了"，不是"模型答得好不好"；
- 有模型端点时同一套题走 LLM 作答器（`--with-model`），两组结果分列，不混算。
"""

from __future__ import annotations

from hippocampus.eval.questions import QUESTIONS, Question
from hippocampus.eval.report import EvalReport, QuestionResult
from hippocampus.eval.runner import run_bundle

__all__ = ["EvalReport", "QUESTIONS", "Question", "QuestionResult", "run_bundle"]
