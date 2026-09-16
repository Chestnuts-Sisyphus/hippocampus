"""评测结果结构与渲染。

三态数字纪律（记忆里的老规矩）：**通过 / 未通过 / 未测**分开报，不把"没测"混进"通过"。
每条失败都归到四分类之一：检索未召回 / 参数错 / 未及时终止 / 工具错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FAILURE_KINDS = ("检索未召回", "参数错", "未及时终止", "工具错")

BOUNDARY = (
    "边界声明：本表为**最小版评测**——题量 10（7 问 3 动作）、单模型端点、单轮次；"
    "离线档作答器为规则作答器（不是模型推理），只测'记忆层有没有把依据给出来'；"
    "样本是**合成数据**，只证方法可复现，效果结论不作普适承诺。"
)


@dataclass
class QuestionResult:
    qid: str
    kind: str
    ok: bool
    steps: int
    failure: str = ""
    injected: list[str] = field(default_factory=list)
    hit_expect: list[str] = field(default_factory=list)
    missed_expect: list[str] = field(default_factory=list)
    auto_label: list[str] = field(default_factory=list)
    answer: str = ""
    note: str = ""


@dataclass
class ArmResult:
    """一条臂（记忆开 / 记忆关）的结果。"""

    name: str
    passed: int = 0
    total: int = 0
    steps: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    results: list[QuestionResult] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def avg_steps(self) -> float:
        return (self.steps / self.total) if self.total else 0.0


@dataclass
class EvalReport:
    arms: list[ArmResult] = field(default_factory=list)
    label_agreement: float = 0.0
    label_pairs: int = 0
    boundary: str = BOUNDARY
    mode: str = "offline"
    model: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model,
            "boundary": self.boundary,
            "label_agreement": round(self.label_agreement, 4),
            "label_pairs": self.label_pairs,
            "notes": self.notes,
            "arms": [
                {
                    "name": arm.name,
                    "passed": arm.passed,
                    "total": arm.total,
                    "success_rate": round(arm.success_rate, 4),
                    "avg_steps": round(arm.avg_steps, 3),
                    "failures": arm.failures,
                    "results": [r.__dict__ for r in arm.results],
                }
                for arm in self.arms
            ],
        }

    def render(self) -> str:
        lines: list[str] = []
        for arm in self.arms:
            head = f"[{arm.name}] 通过 {arm.passed}/{arm.total}"
            if arm.total:
                head += f"（成功率 {arm.success_rate:.0%}，平均 {arm.avg_steps:.1f} 步）"
            lines.append(head)
            if arm.failures:
                detail = " / ".join(f"{k} {v}" for k, v in arm.failures.items() if v)
                lines.append(f"    失败分类：{detail}")
            for r in arm.results:
                mark = "✓" if r.ok else "✗"
                extra = f"  ← {r.failure}" if r.failure else ""
                lines.append(f"    {mark} {r.qid} [{r.kind}] {r.steps} 步{extra}")
        if self.arms:
            lines.append("[标签双来源] 一致率 " + f"{self.label_agreement:.0%}（{self.label_pairs} 题可比）")
        for note in self.notes:
            lines.append(f"注：{note}")
        lines.append(self.boundary)
        return "\n".join(lines)


__all__ = ["BOUNDARY", "FAILURE_KINDS", "ArmResult", "EvalReport", "QuestionResult"]
