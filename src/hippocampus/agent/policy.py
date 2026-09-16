"""编排策略：`think` 节点怎么决定下一步。

两条路，**同一张图、同一套工具**（同一代码路径，只换决策来源）：
- `RulePolicy`（离线档）：先给任务定意图，再按"检索 → 动作 → 收口"的固定节奏走。
  它不是模型推理，能力边界写在文档与出错信息里；
- `ModelPolicy`（配了模型端点）：把工具描述与记忆片段给模型，让它输出下一步动作。

**证据闸**（离线档的关键纪律）：检索到东西 ≠ 有依据。检索结果里**最高分低于本档
证据线**（calibration 的 `answer_floor`）时按"无依据"处理——宁说不知道，不许拿不相干的
记忆硬答（"记错的 agent 越用越歪"这条纪律的运行时版本）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ACTIONS = ("search_memory", "remember", "list_memories", "write_file", "answer")

_REMEMBER_RE = re.compile(r"(?:记下|记住|记录|帮我记)[:：,，]?\s*(.+)", re.S)
# 内容在前的中文语序：「把 X 这条偏好记下来」
_PUT_FIRST_RE = re.compile(r"把\s*(.+?)\s*(?:这(?:条|个|些)?\s*)?(?:偏好|偏好项|记忆|事实)?\s*(?:记下来|记下|记住|记录下来|记一下|记录一下)")
# 收尾填充词（"并确认已入库""到记忆里"这类，不属于记忆内容）
_FILLER_RE = re.compile(r"(并确认已入库|并确认|到记忆里|进去|下来|一下|这条偏好|这个偏好)[。.!！]?")
_WRITE_RE = re.compile(r"(写成|导出|保存到|存到|落成|写成文件|markdown 文件)")
_LIST_RE = re.compile(r"(列出|列一下|有哪些|清单)")
_QUOTED_RE = re.compile(r"[「『\"']([^」』\"']{2,200})[」』\"']")


@dataclass
class Decision:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "args": self.args, "why": self.why}


def classify_intent(task: str) -> str:
    """任务意图（**顺序即优先级**：问句 > 写文件 > 记下 > 列清单 > 问答）。

    顺序有讲究：
    - 以问号收尾的任务**一律当问答**（"我有哪些硬性限制？"是提问，不是让我列清单）；
    - 动作词里"写文件"比"列清单"更具体：'把我的清单写成文件' 必须判成写文件，
      否则会被列清单抢走、动作落不了地。
    """
    text = (task or "").strip()
    if text.endswith(("？", "?")):
        return "qa"
    if _WRITE_RE.search(text):
        return "write_file"
    if _REMEMBER_RE.search(text):
        return "remember"
    if _LIST_RE.search(text):
        return "list_memories"
    return "qa"


class RulePolicy:
    """离线档策略（零模型、确定性）。

    节奏：第 1 步检索（拿依据）→ 第 2 步执行意图动作（如果需要）→ 收口作答。
    """

    name = "rule"

    def __init__(self, answer_floor: float | None = None) -> None:
        self.answer_floor = answer_floor
        self._last_query = ""

    # 证据闸：分数够不够格当"依据"
    def floor(self) -> float:
        if self.answer_floor is not None:
            return self.answer_floor
        try:
            from hippocampus.settings import embedding_config, tier_params

            tier = tier_params(embedding_config()["model"])
            return float(tier.get("answer_floor", 0.2))
        except Exception:
            return 0.2

    def has_evidence(self, memory_lines: list[str], scored: list[tuple[float, str]] | None) -> bool:
        """有依据 = 分数过证据线 **且** 与问题确有关键词重合。

        两条都要，缺一不可：
        - **分数闸**：图通道的分数是离散的（0.5/0.3/0.15）且会被"会话实体兜底"带上
          本会话的实体；它表示"跟某个实体有关"，不表示"回答了这个问题"。所以只有
          语义／关键词／事件线索通道的分数拿来过闸。
        - **重合闸**：事件线索通道会被「上次」「之前」这类**会话指代词**触发，
          于是问"我上次提到的那本书叫什么"也能捞出一堆本会话的旧偏好。捞出来可以注入
          （这是"跨会话接着做"的能力），但**不能当作"回答了这个问题"的依据**——
          必须与问题有实义关键词重合（排除指代词与功能字）。
        """
        if not memory_lines or not scored:
            return False
        best = max((s for s, ch in scored if ch != "graph"), default=0.0)
        if best < self.floor():
            return False
        return any(_shares_content_keyword(self._last_query, line) for line in memory_lines)

    def decide(
        self,
        task: str,
        *,
        step: int,
        memory_lines: list[str],
        scored: list[tuple[float, str]] | None = None,
        last_tool: dict[str, Any] | None,
        tools: list[dict[str, Any]],
    ) -> Decision:
        available = {t["name"] for t in tools}
        intent = classify_intent(task)
        self._last_query = task
        has_evidence = self.has_evidence(memory_lines, scored)

        # 第 1 步：先检索（问答类即使无依据也要先看一眼，才知道有没有）
        if step <= 1 and "search_memory" in available and task:
            return Decision("search_memory", {"query": task, "limit": 5}, "先找依据（检索记忆）")

        # 第 2 步：按意图执行动作（写文件类即使没有检索结果也让用户看到导出内容）
        if step == 2:
            if intent == "remember" and "remember" in available:
                payload = _remember_payload(task)
                return Decision("remember", {"content": payload[:200], "kind": _guess_kind(payload)}, "任务要求记下")
            if intent == "list_memories" and "list_memories" in available:
                return Decision("list_memories", {"kind": "preference", "limit": 20}, "任务要求列清单")
            if intent == "write_file" and "write_file" in available:
                # 写文件类**不以"检索到证据"为前提**：用户明确要求导出，就把现有内容导出；
                # 内容空不空是结果问题，不是该不该动手的问题（危险动作的闸在工具层的确认
                # 机制，不在证据闸）。这样"放行/拒绝"只取决于确认状态，不取决于索引时机。
                body = "\n".join(memory_lines)
                return Decision(
                    "write_file",
                    {"path": "memory-export.md", "content": f"# 记忆导出\n\n{body}\n"},
                    "任务要求写文件",
                )

        # 收口
        if has_evidence:
            return Decision("answer", {"text": _compose(memory_lines)}, "有依据，可回答")
        return Decision(
            "answer",
            {"text": "我的记忆里没有相关信息，无法确认。", "insufficient": True},
            "检索结果不足以作为依据" if memory_lines else "没有检索到相关记忆",
        )


def _compose(lines: list[str]) -> str:
    return "根据我的记忆：" + "；".join(lines)


def _remember_payload(task: str) -> str:
    """从"把 X 记下来"这类指令里取出**要记的 X**。

    中文语序有两种，都要覆盖（否则会把"记下来"的"来"当成内容入库——实测踩过）：
      ① 内容在前：「把 X 这条偏好记下来」→ X
      ② 内容在后：「记住：X」→ X
    """
    text = (task or "").strip()
    quoted = _QUOTED_RE.search(text)
    if quoted:
        return quoted.group(1).strip()
    m = _PUT_FIRST_RE.search(text)
    if m:
        payload = m.group(1).strip()
    else:
        m2 = _REMEMBER_RE.search(text)
        payload = m2.group(1).strip() if m2 else text
    payload = _FILLER_RE.sub("", payload)
    return payload.strip().strip("：:，,。.！!").strip()


def _guess_kind(content: str) -> str:
    if any(w in content for w in ("喜欢", "不喜欢", "讨厌", "只看", "优先", "不要", "不想", "避免", "必须", "不投")):
        return "preference"
    if any(w in content for w in ("已完成", "正在", "在准备", "最近")):
        return "status"
    if any(w in content for w in ("配置", "文件", "路径", "地址", "文档")):
        return "resource"
    return "fact"


class ModelPolicy:
    """有模型端点时：把工具描述 + 记忆片段交给模型决策（OpenAI 兼容端点）。"""

    name = "model"

    def __init__(self, model: str = "", answer_floor: float | None = None) -> None:
        self.model = model
        self.answer_floor = answer_floor

    def decide(
        self,
        task: str,
        *,
        step: int,
        memory_lines: list[str],
        scored: list[tuple[float, str]] | None = None,
        last_tool: dict[str, Any] | None,
        tools: list[dict[str, Any]],
    ) -> Decision:
        from hippocampus.settings import llm_available, llm_chat_json

        if not llm_available():
            raise RuntimeError("model policy 需要模型端点（请配 base_url 与凭据，或改用 --offline）")
        system = (
            "你是记忆驱动的 agent 的决策节点。只输出 JSON：\n"
            '{"action": "search_memory|remember|list_memories|write_file|answer", "args": {...}, "why": "一句话"}\n'
            "规则：没有依据就说不知道（action=answer, args.insufficient=true），不要编。"
        )
        memory_block = "\n".join(memory_lines) if memory_lines else "（无）"
        user = (
            f"任务：{task}\n第 {step} 步\n可用工具：{json.dumps(tools, ensure_ascii=False)}\n"
            f"已注入记忆：\n{memory_block}\n"
            f"上一步结果：{json.dumps(last_tool or {}, ensure_ascii=False)[:800]}"
        )
        data = llm_chat_json(system, user, max_tokens=600)
        action = str(data.get("action") or "answer")
        if action not in ACTIONS:
            action = "answer"
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return Decision(action, args, str(data.get("why") or ""))


def _shares_content_keyword(query: str, memory_line: str) -> bool:
    """问题与记忆行是否有**实义**字面重合（2 字窗口，排除指代/功能字）。

    为什么不用分词：这里要的是"有没有说到同一件事"的廉价判据，不是语义理解。
    排除字表覆盖指代词（我／你／上次／什么）与高频功能字，避免"我…的"这种
    伪重合把无关记忆抬成依据。
    """
    left = set(_content_grams(query))
    right = set(_content_grams(memory_line))
    return bool(left & right)


_STOP_CHARS = set("我你他她它您的了是在有和与就都也还很太什么怎么这那上次下次一下请问帮我我们你们他们这个那个一个可以要不")


def _content_grams(text: str) -> list[str]:
    chars = [c for c in (text or "") if not c.isspace() and c not in "，。！？；：、（）【】《》「」『』,.:;!?()[]{}<>\"'"]
    out = []
    for i in range(len(chars) - 1):
        gram = chars[i] + chars[i + 1]
        if all(c in _STOP_CHARS for c in gram):
            continue
        out.append(gram)
    return out


__all__ = ["ACTIONS", "Decision", "ModelPolicy", "RulePolicy", "classify_intent"]
