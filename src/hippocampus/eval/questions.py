"""评测题集（投递前最小版 10 题：7 题问答 + 3 题带工具动作）。

每题三件事必须齐全（"任务双必需原则"，方案 §十八-1）：
1. `expect`：**没有记忆就答不对**——答题要点写死在目标记忆里，记忆关掉就缺依据；
2. `tool`：动作题**没有工具就落不了地**（写文件／列目录／查记忆）；
3. `label`：标签双来源的比较基准（人工写的真值），与运行时的自动标签对齐率另行计算。

题目环境由 `hippocampus seed` 的合成数据提供（src/hippocampus/seed.py），
每条 `expect` 都能在 SEED_ITEMS 里找到。
"""

from __future__ import annotations

from dataclasses import dataclass, field

QA = "qa"
TOOL = "tool"


@dataclass
class Question:
    qid: str
    text: str
    kind: str
    expect: list[str] = field(default_factory=list)   # 人工标签：应被注入/引用的记忆要点
    tool: str = ""                                     # 动作题要用的工具名
    note: str = ""
    # 判分口径（动作题不能只看回答文本，要看**产物**）：
    #   answer        作答文本里出现要点
    #   memory_exists 库里存在该内容的记忆（"记下来"类任务的产物）
    #   file_written  写出的文件里含该内容（"导出"类任务的产物）
    #   refusal       应当**拒答**（记忆里没有对应事实；考"不许编"）
    check: str = "answer"
    # 前置条件（评测开始前要建立的状态）：
    #   ""              不需要
    #   "resolve_pending" 先把示例数据里的冲突挂起做出裁决（q04 测的是"裁决之后"的行为）
    setup: str = ""
    # 数据来源（C2，报告分列"合成/真实"）：
    #   synthetic  合成示例数据（seed 生成，证方法可复现）
    #   real-jd    真实岗位 JD 派生场景（**脱敏**：不包含任何私人 JD 原文，只保留
    #               招聘方普遍考察的 agent 能力要求，如记忆/工具/拒答/多步任务）
    source: str = "synthetic"


QUESTIONS: list[Question] = [
    # ---- 七成：记忆驱动的问答 ----
    Question(
        "q01",
        "我投简历之前有什么必须做的检查？",
        QA,
        expect=["简历投递前必须先过一遍错别字"],
        note="单条偏好召回",
    ),
    Question(
        "q02",
        "我找岗位时有哪些硬性限制？",
        QA,
        expect=["我只看允许远程的岗位", "我不投需要长期出差的岗位"],
        note="多条并列偏好召回",
    ),
    Question(
        "q03",
        "我技术面之前一般会做什么准备？",
        QA,
        expect=["技术面之前我要先做一遍同岗位真题"],
    ),
    Question(
        "q04",
        "我现在想去哪个城市工作？",
        QA,
        expect=["我的期望城市是杭州"],
        note="跨会话改口后应给新值（前置：先对挂起的城市冲突做裁决）",
        setup="resolve_pending",
    ),
    Question(
        "q05",
        "我的目标岗位方向是什么？",
        QA,
        expect=["我的目标岗位方向是后端开发与基础设施"],
    ),
    Question(
        "q06",
        "我每周固定留给自己项目的时间是哪天？",
        QA,
        expect=["我每周三晚上固定留给项目开发"],
    ),
    Question(
        "q07",
        "我上次提到的那本书叫什么名字？",
        QA,
        expect=[],
        note="负样本对照：记忆里没有这本书，正确行为是拒答（不许编）",
        check="refusal",
    ),
    # ---- 三成：带工具的动作 ----
    Question(
        "q08",
        "把'我不投需要长期出差的岗位'这条偏好记下来并确认已入库",
        TOOL,
        expect=["我不投需要长期出差的岗位"],
        tool="remember",
        note="写记忆工具（动作落地）——判分看**产物**：库里真的存在这条记忆",
        check="memory_exists",
    ),
    Question(
        "q09",
        "列出当前生效的岗位相关偏好清单",
        TOOL,
        expect=["我只看允许远程的岗位", "简历投递前必须先过一遍错别字"],
        tool="list_memories",
    ),
    Question(
        "q10",
        "把我的岗位偏好清单写成一个 markdown 文件",
        TOOL,
        expect=["我只看允许远程的岗位"],
        tool="write_file",
        note="写文件＝危险动作，需确认——判分看**产物**：写出的文件里有这条内容",
        check="file_written",
    ),
    # ---- 第二轮（N8 扩到 20 题；real-jd = 真实 JD 派生场景，脱敏）----
    Question(
        "q11",
        "我有哪些记忆系统项目经验？",
        QA,
        expect=["我有记忆系统项目经验", "会工具调用和多步任务"],
        note="real-jd：agent 岗位普遍要求记忆/工具调用经验（脱敏场景）",
        source="real-jd",
    ),
    Question(
        "q12",
        "我做记忆系统的核心要点是什么？",
        QA,
        expect=["记忆要长期保存", "模型输出不能污染记忆", "检索要保证召回"],
        note="real-jd：跨会话记忆是 agent 岗高频考点（脱敏场景）",
        source="real-jd",
    ),
    Question(
        "q13",
        "我对候选岗位的技术栈有什么偏好？",
        QA,
        expect=["我优先投 Python 技术栈的公司"],
    ),
    Question(
        "q14",
        "我做过向量检索与召回吗？",
        QA,
        expect=["我做过向量检索与召回"],
        note="real-jd：JD 里的 RAG 要求对应我的记忆核心检索经验（脱敏）",
        source="real-jd",
    ),
    Question(
        "q15",
        "我的理想薪资是多少？",
        QA,
        expect=[],
        note="负样本对照：记忆里没有薪资信息，正确行为是拒答（不许编）",
        check="refusal",
        source="real-jd",
    ),
    Question(
        "q16",
        "我平时用什么工具管理笔记？",
        QA,
        expect=["我用 Obsidian 管理笔记"],
    ),
    Question(
        "q17",
        "我的记忆系统怎么让记忆不过期？",
        QA,
        expect=["生命周期管理让记忆过期", "离线固化保存长期记忆"],
        note="real-jd：'怎么让 agent 不遗忘'是 agent 岗高频考点（脱敏场景）",
        source="real-jd",
    ),
    Question(
        "q18",
        "把'我优先投 Python 技术栈的公司'记下来并确认已入库",
        TOOL,
        expect=["我优先投 Python 技术栈的公司"],
        tool="remember",
        check="memory_exists",
    ),
    Question(
        "q19",
        "列出我关于岗位筛选的全部偏好",
        TOOL,
        expect=["我只看允许远程的岗位", "我不投需要长期出差的岗位", "我优先投 Python 技术栈的公司"],
        tool="list_memories",
    ),
    Question(
        "q20",
        "把'我的技术栈偏好'整理成一个 markdown 文件",
        TOOL,
        expect=["我优先投 Python 技术栈的公司"],
        tool="write_file",
        check="file_written",
    ),
]

ANSWER_TEMPLATE = "根据我的记忆：{points}"


def questions_for(limit: int) -> list[Question]:
    """取前 limit 题（按 kind 交错，保证问答与动作都在样本里）。"""
    if limit <= 0 or limit >= len(QUESTIONS):
        return list(QUESTIONS)
    qa = [q for q in QUESTIONS if q.kind == QA]
    tool = [q for q in QUESTIONS if q.kind == TOOL]
    out: list[Question] = []
    ratio = len(qa) / max(len(QUESTIONS), 1)
    ci = ti = 0
    while len(out) < limit:
        want_qa = (len(out) + 1) * ratio > len([q for q in out if q.kind == QA])
        if want_qa and ci < len(qa):
            out.append(qa[ci])
            ci += 1
        elif ti < len(tool):
            out.append(tool[ti])
            ti += 1
        elif ci < len(qa):
            out.append(qa[ci])
            ci += 1
        else:
            break
    return out


__all__ = ["ANSWER_TEMPLATE", "QA", "QUESTIONS", "TOOL", "Question", "questions_for"]
