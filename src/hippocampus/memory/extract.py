# 由前身 hippocampus_prototype/extract.py 抽取移植（vendoring），仅做包内 import 改写与全局依赖解耦。
# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。

"""
Hippocampus 原型 —— 实体提取 + 记忆提取（问题 #1 验证）
对应设计文档 v2.1：提取阶段 ADD-only，实体判断用两次二分法，记忆分四类
输出统一为紧凑 JSON，由 llm.chat_json 强校验

任务书 8a 加固（2026-08-07）：
- 动态 max_tokens：min(8000, 2000 + len(user_text)//2)，长消息不再被 2000 截断
- 失败重试：chat_json 抛异常（JSON 解析失败/网络错误）自动重试 1 次，
  重试 max_tokens ×1.5（封顶 8000）
- 分块提取：user_text >2500 字符时按段落切成 ≤2000 字符的块，每块单独
  提取（同样动态 max_tokens + 重试）；entities 按 name 去重（保留第一个），
  memories 全量合并；后续块附「已知实体名」提示保持跨块实体命名一致
- extract() 签名与输出格式不变，旧调用方零改动兼容
"""

import re
from typing import Any

from hippocampus.memory.llm import chat_json

# P1（HC-0815-02 节点1）：瞬时状态模式（机械拦截 + 生命周期降级共用，防双源漂移）。
# 高置信模式：HEAD / commit 哈希 / 完整 40 位哈希 / run·构建·任务号。只匹配 status 类。
# 6e（节点6，§2c 收口）：补完成态任务断言（已完成/已交付/已上线/已发布/已验收/已部署）——
# 「部署已完成」类任务进度快照机械拦截；「这个 bug 已经修复了」不含这些词，必须留下。
TRANSIENT_STATUS_RE = re.compile(
    r"\bHEAD\b"
    r"|\bcommit\b\s+[0-9a-fA-F]{7,40}"
    r"|\b[0-9a-fA-F]{40}\b"
    r"|\b(?:run|RUN|构建|build)\s*[#：:号]?\s*\d{1,6}"
    r"|\b任务描述\b"
    r"|已完成|已交付|已上线|已发布|已验收|已部署",
    re.IGNORECASE,
)


def is_transient_status(mem: dict) -> bool:
    """瞬时状态判定（机械兜底，不依赖 LLM 听话）：type=status 且内容/来源
    匹配瞬时模式（HEAD 哈希/commit/run 号等）→ 不入库。非 status 一律 False。"""
    if not isinstance(mem, dict):
        return False
    if mem.get("type") != "status":
        return False
    text = f"{mem.get('content', '')} {mem.get('source_quote', '')}"
    return bool(TRANSIENT_STATUS_RE.search(text or ""))


# P2（HC-0815-02 节点2）：情绪宣泄模式（type=status 且纯主观情绪表达 → 噪声不入库）。
# 只匹配「情绪词 + 完整体」的高置信模式，避免误伤「这个 bug 修好了，但过程太烦了」类
# 含情绪但有事实信息的句子（这类不以情绪词结尾收束）。
EMOTIONAL_STATUS_RE = re.compile(
    r"(?:太烦了|烦死了|烦得要死|好累|累死了|累得不行|崩溃了|要崩溃|受不了|"
    r"气死了|气死我了|无语了|难受死了|不想干了|不想做了|折腾死|整吐了|"
    r"头都大了|心态炸了|麻了)$"
)


def is_emotional_status(mem: dict) -> bool:
    """情绪宣泄判定：type=status 且内容以情绪宣泄句式结尾（「查 bug 三小时太烦了」）。
    命中=噪声不入库（机械兜底，与瞬时状态同层）。非 status 一律 False。"""
    if not isinstance(mem, dict):
        return False
    if mem.get("type") != "status":
        return False
    text = f"{mem.get('content', '')} {mem.get('source_quote', '')}".strip()
    return bool(EMOTIONAL_STATUS_RE.search(text or ""))


# P2（HC-0815-02 节点2）：记忆类型白名单（LLM 输出校验，防类型泄漏/乱造类型）。
# 观察线实测：status 混入轨道A 47%（发现 16 更正版）；LLM 可能输出 task/goal 等
# 非法类型 → 机械剔除，保证入库类型只四类。
VALID_MEMORY_TYPES = ("preference", "fact", "resource", "status")


def filter_valid_memories(memories: list[dict]) -> list[dict]:
    """类型白名单过滤：剔除 type 不在四类内的记忆（轨道A/B 共用，防类型泄漏）。"""
    if not memories:
        return []
    return [m for m in memories if isinstance(m, dict) and m.get("type", "fact") in VALID_MEMORY_TYPES]


# ---------------------------------------------------------------------------
# [HIPPO] 离线档的句式规则抽取（验收 A36）
# ---------------------------------------------------------------------------
# 为什么要有：前身所有抽取都走 LLM。本项目要求"无 key／无网也能跑记忆纪律"，
# 所以必须有**不依赖模型**的抽取通道。它只认显式句式——宁可少抽，不许抽错
# （判断句式的确凿性是它唯一的正确性来源；能力边界写在 docs/offline.md）。

_RULE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 偏好：带"我"的显式表达（含"我只投/我只看/我优先"这类行为约束句）
    (
        re.compile(
            r"^我(?:只|也|更|最|很|不|不太|非常|特别|总是|通常|一般|还是|会)?"
            r"(?:喜欢|偏好|看好|讨厌|反感|习惯|倾向|希望|想|要|需要|不接受|不考虑|不去|不投|不看|"
            r"不买|避免|优先|选择|用|使用|投|做|看|学|吃|住|去|玩|追)"
        ),
        "preference",
    ),
    (re.compile(r"^我(?:不|没|别|拒绝|禁止|别再|不再)"), "preference"),
    (re.compile(r"(?:以后|每次|一律|所有|凡是|一定)"), "preference"),
    # 事实：主系表式陈述（"我的期望城市是北京""我是在校生"）
    (re.compile(r"^我(?:的)?\S{1,12}(?:是|叫|属于|来自)"), "fact"),
    # 资源：指向位置/文件/工具
    (re.compile(r"(?:在|位于|存在|放在)\s*[A-Za-z]:[\\/]|(?:配置在|写在|放在)\s*\S+\.(?:json|yaml|yml|toml|md|py|txt|ini)"), "resource"),
    # 状态：当前进度，会变化
    (re.compile(r"(?:正在|目前|当前|现在)\S{0,20}(?:做|写|开发|准备|推进|学习|刷|看)|(?:已完成|已交付|已上线|已部署|做完了)"), "status"),
]
_RULE_ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}|[\u4e00-\u9fff]{2,6}(?=的|在|是|用|要|不)")
_QUOTE = "「」『』\"'“”"


def _rule_entities(text: str) -> list[dict[str, Any]]:
    """粗粒度实体名（离线档只做"能在检索里被提到"的程度，不做类型细分）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _RULE_ENTITY_RE.findall(text or ""):
        name = raw.strip()
        if len(name) < 2 or name in seen or name in _RULE_STOPWORDS:
            continue
        seen.add(name)
        out.append({"name": name, "type": "Abstract"})
        if len(out) >= 6:
            break
    return out


_RULE_STOPWORDS = frozenset(
    {
        "这个", "那个", "什么", "怎么", "可以", "应该", "需要", "因为", "所以", "但是", "如果",
        "我的", "你的", "他的", "我们", "你们", "他们", "一下", "一个", "就是", "还是", "这样",
    }
)


def extract_by_rules(text: str) -> dict[str, Any]:
    """句式规则抽取（离线档）：按句切分 → 逐句判类型 → 产出同一格式。

    只抽**显式陈述句**：句子以"我"开头且命中偏好/状态/资源句式，或整句命中完成态句式。

    两条硬边界（都是实测踩出来的）：

    - **疑问句一律不抽**。踩过的坑：切句时把"？"当分隔符丢掉了，于是
      「我投简历有什么要求？」被剥成「我投简历有什么要求」→ 命中"我+投"句式被当成偏好入库；
      下一轮用户问同一句话时，这条"记忆"与查询逐字相似（sim 0.95）→ 独占语义通道 +
      被"防重复注入已见内容"过滤 → **注入被挤成 0**。所以这里保留句末标点判疑问，
      并检查句中疑问词（不只查句首）。
    - **祈使句不抽**（"帮我…""把…写成文件""列出…"）：那是任务指令，不是关于用户的陈述。
    """
    body = (text or "").strip()
    if not body:
        return {"entities": [], "memories": []}
    # 保留句末标点（capturing 切分）：疑问号是判"这句是不是问题"的唯一可靠线索
    sentences = [s.strip() for s in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", body) if s.strip()]
    memories: list[dict[str, Any]] = []
    for raw in sentences:
        if _is_question(raw) or _is_imperative(raw):
            continue
        sentence = raw.rstrip("。！!；;").strip()  # 入库内容不带句末标点（问号句前面已排除）
        if len(sentence) < 4 or len(sentence) > 200:
            continue
        kind = ""
        for pattern, mounted in _RULE_PATTERNS:
            if pattern.search(sentence):
                kind = mounted
                break
        if not kind:
            continue
        memories.append(
            {
                "type": kind,
                "content": sentence,
                "entity_names": [e["name"] for e in _rule_entities(sentence)],
                "source_quote": sentence,
            }
        )
    memories = filter_valid_memories(memories)
    entities = _rule_entities(body)
    return {"entities": entities, "memories": memories}


# 疑问标记：句末标点 + 句中疑问词（不只查句首——"我投简历有什么要求"这类疑问词在中段）
_QUESTION_WORDS = (
    "什么", "怎么", "怎样", "如何", "为什么", "为啥", "哪些", "哪个", "哪家", "哪里", "哪儿",
    "多少", "多久", "多长", "几点", "几个", "几号", "谁", "是不是", "有没有", "要不要",
    "吗", "呢", "咋", "何时", "是否", "能不能", "可不可以", "好不好",
)
# 祈使/任务指令开头（那是让我干活，不是关于用户的陈述）
_IMPERATIVE_HEADS = (
    "帮我", "请帮", "请", "把", "给我", "列出", "列一下", "导出", "生成", "整理", "写一个",
    "写个", "做成", "跑一下", "执行", "查一下", "看看", "找一下", "搜一下",
)


def _is_question(sentence: str) -> bool:
    """是不是疑问句（句末问号，或句中带疑问词）。"""
    text = (sentence or "").strip()
    if not text:
        return True
    if text.endswith(("？", "?")):
        return True
    return any(word in text for word in _QUESTION_WORDS)


def _is_imperative(sentence: str) -> bool:
    """是不是祈使/任务指令句。"""
    text = (sentence or "").strip()
    return any(text.startswith(head) for head in _IMPERATIVE_HEADS)


# 提取系统提示：复用 Mem0 V3 精华（When in doubt extract / 穷举检查清单 / 反首题主导）
EXTRACT_SYSTEM = """你是记忆提取引擎，为"像人一样有记忆的AI"服务。

你的任务：从用户的对话中提取（1）实体（2）记忆。

【实体判断标准】用户能否用"那个X"独立指代它？能→是实体。不能→不是实体，是属性，不提取。
【实体类型·两次二分法】
第一问：它是"发生的事情"还是"存在的东西"？发生的事情→Event（事件，如"写代码"）。
第二问（存在的东西）：有物理形态吗？有→Concrete（具象）。没有→Abstract（抽象）。
【实体名规范·重要】实体名必须是核心名词，不带修饰语。"X的Y"结构必须拆成两个实体（如"Hippocampus的存储结构"→"Hippocampus"和"存储结构"两个实体）。
【实体提取原则】
- When in doubt, extract：宁可多提取也不漏提取，去重交给下游
- 穷举检查：用户说的每条消息、中间和末尾的内容都要覆盖
- 反首题主导：不要只关注第一个话题，对话可能涉及多个维度
- 休闲话题也要提取，不是 chitchat 就可以跳过

【记忆提取原则】
- 只提取稳定的、值得长期记住的信息（偏好/事实/资源/状态）
- 判别问题（优先级最高，先回答这四个问题再定类型）：
  · preference：这句话约束「以后/每次」的行为方式吗？能说「我希望你以后都这样」吗？
  · fact：这是不依赖当前计划、也不指挥行为的客观陈述吗？
  · resource：是否指向某个位置、文件、工具或人的去处？
  · status：是否描述「当前」状态、将来会变化？
- 记忆类型：preference（偏好，行为约束）| fact（事实，客观信息）| resource（资源位置）| status（项目状态，会变化）——【类型只能这四种，禁止输出其他类型（task/goal/plan/progress 等一律不输出）】
- 正反例（正例=该类典型句，反例=最像该类但实属别类）：
  · preference 正例：「我不喜欢打补丁式设计」 反例：「Hippocampus 的存储结构设计得差不多了」（描述当前进度，是 status 不是偏好）
  · fact 正例：「Python 是解释型语言」
  · fact 反例：「我不喜欢打补丁式设计」（含主观喜恶，是 preference 不是事实）；「把开源发布定在 10 月之前」（当前计划，是 status 不是事实）
  · resource 正例：「密钥存在 config.yaml 里」 反例：「我用的是 deepseek 的 API」（陈述使用什么，是 fact 不是资源位置）
  · status 正例：「这个 bug 已经修复了」「开源发布定在 10 月之前，需先完成 3 个真实用户稳定使用 2 周」（当前计划/门槛） 反例：「明天记得把设计方案备份到 Obsidian」（是行为指令不是当前状态，是 preference 不是 status）
- 【情绪宣泄·不提取】：纯主观情绪发泄不提取（「查 bug 三小时太烦了」「累死了」）——没有长期信息，是噪声不是 status
- 总原则：拿不准 preference 还是 fact 时，问「这句话约束未来行为吗」——约束→preference，不约束→fact
- 总原则：拿不准 fact 还是 status 时，问「以后会不会被改写成另一版计划/进度/门槛」——会→status；不要用改得勤不勤代替
- 【瞬时状态/任务描述·不提取】（P1，2026-08-16）：以下内容一律不提取，会很快失效、污染记忆库：
  · 版本号/HEAD/commit/哈希类（如「HEAD 在 1a2b3c4」「commit abc123 改了检索」）——换 commit 即失效
  · 运行号/任务号/构建号（如「run 42 失败了」「构建 #17 通过」）——一次性运行信息
  · 完成态任务断言（如「已完成 X」「已交付 X」「X 设计得差不多了」）——当前计划的临时状态，不值得长期记住
  注意：**进行中的进度状态必须提取为 status**（「轨道B开发进行到一半」「验收进度完成一半」
  「这个 bug 已经修复了」——将来会变化但有长期意义）；「已完成 X」的区分点在
  「X」是任务/事项（临时清单项）而非结果（bug/功能等长期成果）。
- 【否定偏好·必查】「不要/不能/不该/别/禁止/别用/别管」开头的约束句 = 稳定偏好（约束以后行为方式），必须提取为 preference——哪怕情绪化。
  反例：「不要用之前生成的图当参考图」→ preference（以后别用旧图）；「你不能自己胡编乱造」→ preference（以后要按我的要求来）；「不该有太多明暗对比」→ preference（以后明暗对比少一点）。
- 每条记忆必须带 source_quote（用户原话片段）
- 判断该记忆关联到最具体的实体（防 mega-hub：关联"存储结构"而非"Hippocampus"）

【输出格式】只输出一个紧凑 JSON 对象（不要缩进、不要多余换行）：
{"entities":[{"name":"实体名","type":"Concrete|Abstract|Event","aliases":["别名"]}],
 "memories":[{"type":"preference|fact|resource|status","content":"记忆内容","entity_names":["关联实体名"],"source_quote":"用户原话"}]}
没有就输出空数组。"""

# 任务书 8a：输出 token 上限（动态放大封顶值）
_MAX_OUTPUT_TOKENS = 8000
# 长文本分块阈值：超过则分块提取
_CHUNK_THRESHOLD = 2500
# 单块最大字符数
_CHUNK_MAX_CHARS = 2000
# 失败块二次细分下限：低于此长度不再细分（直接抛异常）
_SUBSPLIT_MIN = 500


def _dynamic_max_tokens(user_text: str) -> int:
    """按输入长度估算输出上限：min(8000, 2000 + len(user_text)//2)"""
    return min(_MAX_OUTPUT_TOKENS, 2000 + len(user_text) // 2)


def _chat_json_with_retry(system: str, user: str, max_tokens: int) -> dict[str, Any]:
    """调用 chat_json，失败自动重试 1 次（重试 max_tokens ×1.5 封顶 8000）。

    覆盖 JSON 解析失败（截断）与网络错误；两次都失败才抛异常。
    """
    try:
        return chat_json(system, user, max_tokens=max_tokens)
    except Exception:
        retry_tokens = min(_MAX_OUTPUT_TOKENS, int(max_tokens * 1.5))
        return chat_json(system, user, max_tokens=retry_tokens)


def _split_chunks(text: str) -> list[str]:
    """按段落（换行）切块，每块 ≤2000 字符；单段超长硬切。

    返回非空块列表（输入全空时返回 [text] 由调用方语义兜底）。
    """
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if cur_len + len(para) + 1 > _CHUNK_MAX_CHARS and cur:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        if len(para) > _CHUNK_MAX_CHARS:
            # 超长单段：先清空当前块，再硬切
            if cur:
                chunks.append("\n".join(cur))
                cur = []
                cur_len = 0
            while len(para) > _CHUNK_MAX_CHARS:
                chunks.append(para[:_CHUNK_MAX_CHARS])
                para = para[_CHUNK_MAX_CHARS:]
        cur.append(para)
        cur_len += len(para) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks if chunks else [text]


def _halve(text: str) -> list[str]:
    """按段落边界对半切（优先最近的换行），返回 2 段（均为非空）。"""
    mid = len(text) // 2
    nl = text.find("\n", mid)
    prev_nl = text.rfind("\n", 0, mid)
    if nl != -1 and (prev_nl == -1 or nl - mid <= mid - prev_nl):
        split_at = nl
    elif prev_nl != -1:
        split_at = prev_nl
    else:
        split_at = mid
    a, b = text[:split_at].strip(), text[split_at:].strip()
    if not a or not b:
        a, b = text[:mid], text[mid:]
    return [a, b]


def _extract_block(text: str) -> dict[str, Any]:
    """单块提取：动态 max_tokens + 失败重试；重试仍失败则二次细分递归提取。

    覆盖「单块输出爆炸」场景（实测 2000 字符密集信息块可触发 LLM 输出
    >4500 tokens 仍截断）：按段落对半细分为 ~1000/500 字符子块后，
    单块输出量级骤降，恢复正常提取。细分后子块结果按实体去重合并。
    """
    try:
        return _chat_json_with_retry(EXTRACT_SYSTEM, text, _dynamic_max_tokens(text))
    except Exception:
        if len(text) <= _SUBSPLIT_MIN:
            raise
        parts = _halve(text)
        entities: list[dict[str, Any]] = []
        memories: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for part in parts:
            prompt = part
            if entities:
                known = "；".join(e["name"] for e in entities[:80])
                prompt = (
                    f"{part}\n\n[参考] 本段前半已提取实体名：{known}\n"
                    "（仅供保持实体命名一致：涉及这些实体时请沿用其名，"
                    "不要重复提取它们，也不要引用本行内容作为 source_quote）"
                )
            r = _extract_block(prompt)
            for ent in r.get("entities", []):
                name = str(ent.get("name", "")).strip()
                if name and name not in seen_names:
                    seen_names.add(name)
                    entities.append(ent)
            memories.extend(r.get("memories", []))
        return {"entities": entities, "memories": memories}


def extract(user_text: str) -> dict[str, Any]:
    """提取实体+记忆，返回 {'entities': [...], 'memories': [...]}

    长文本（>2500 字符）自动分块提取后合并：
    - entities 按 name 去重（同名保留第一个）
    - memories 全量合并
    - 后续块 user 提示附「已知实体名」辅助跨块实体命名一致
    - P0：C/E（密钥/PII）命中则空返回，不调 LLM

    [HIPPO] 离线降级（验收 A36）：无模型端点（未配凭据／离线档）时**不抛异常**，
    改走句式规则抽取 `extract_by_rules`——能力边界写在 docs/offline.md：
    句式规则能抓"我(不)喜欢／只看／优先／讨厌／我是／我在"这类陈述，抓不了隐含表达。
    """
    try:
        from hippocampus.memory import security as secmod

        if secmod.should_hard_block(secmod.check_text(user_text)):
            return {"entities": [], "memories": []}
    except Exception:
        pass
    from hippocampus.memory.llm import LLMUnavailable

    # 注意：这里**不做 llm.available() 预检查**，而是"真调用失败再降级"——
    # 预检查会让"注入进来的模型客户端"（测试替身、用户自定义客户端）永远拿不到调用，
    # 降级要挂在**真实失败**上，不是挂在配置快照上。
    try:
        return _extract_with_llm(user_text)
    except LLMUnavailable:
        return extract_by_rules(user_text)


def _extract_with_llm(user_text: str) -> dict[str, Any]:
    """原有的 LLM 抽取路径（分块 + 重试 + 类型白名单）。"""
    if len(user_text) <= _CHUNK_THRESHOLD:
        # 短文本：直接提取（动态 max_tokens + 失败重试）
        result = _chat_json_with_retry(EXTRACT_SYSTEM, user_text, _dynamic_max_tokens(user_text))
        result["memories"] = filter_valid_memories(result.get("memories", []))
        return result

    # 长文本：分块提取 -> 合并去重
    chunks = _split_chunks(user_text)
    entities: list[dict[str, Any]] = []
    memories: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for chunk in chunks:
        prompt = chunk
        if entities:
            # 附已知实体名，辅助后续块保持实体命名一致（防跨块实体改名/重复）
            known = "；".join(e["name"] for e in entities[:80])
            prompt = (
                f"{chunk}\n\n[参考] 前序块已提取实体名：{known}\n"
                "（仅供保持实体命名一致：后续块涉及这些实体时请沿用其名，"
                "不要重复提取它们，也不要引用本行内容作为 source_quote）"
            )
        result = _extract_block(prompt)
        for ent in result.get("entities", []):
            name = str(ent.get("name", "")).strip()
            if name and name not in seen_names:
                seen_names.add(name)
                entities.append(ent)
        memories.extend(result.get("memories", []))
    return {"entities": entities, "memories": filter_valid_memories(memories)}


# 轨道B专用提取 prompt：从 AI 回复里提取 resource/status（独立于轨道A的用户话提取）
# 关键：区分「AI 陈述的事实/资源/状态」vs「AI 的客套/猜测/表态」，宁缺毋滥。
# 只提取稳定、值得长期记住的资源/状态；每条带 source_quote（AI 原话片段）。
EXTRACT_RESPONSE_SYSTEM = """你是记忆提取引擎，为「把 AI 回复里的资源/状态自动记住」服务。

输入是一段 AI（助手）的回复文本。你的任务：从这段回复中提取值得长期记住的「资源」和「状态」。

【黄金判别】先逐句问：这句话是 AI 在陈述一个稳定的、值得记忆的事实/资源/状态，还是 AI 的客套话/猜测/表态/承诺/建议？
- 客套/猜测/表态/承诺/建议/反问/寒暄 → 一律不提取（宁缺毋滥）。
- 客套例子（不提取）：「我可以帮你改文件」「随时可以问我」「这是我的建议」「希望能帮到你」——
  这些是 AI 的能力声明/客套，不是已发生的资源或状态。
- 猜测例子（不提取）：「可能」「也许」「大概」——不确定的推断不提取。

【资源 resource】指向某个位置、文件、工具、URL、命令、人、API 的去处，AI 明确给出了「在哪」。
- 正例：「密钥在 config.yaml 里」→ resource（给出位置）
- 正例：「文件在 D:/AI/HERMES/hippocampus_prototype/proxy_app.py」→ resource（给出路径）
- 正例：「这个功能在 settings.py 的 Config 类里」→ resource
- 反例（不提取）：「我用的是 DeepSeek 的 API」（陈述用的什么，没给位置，是 fact 不是资源）

【状态 status】描述「当前/近期」某个事情的进展、结果、变化，将来可能不同。
- 正例：「这个 bug 已经修复了」→ status
- 正例：「轨道B开发进行到一半」→ status
- 正例：「测试已通过」「部署已完成」→ status
- 反例（不提取）：「明天记得备份」（行为指令，不是当前状态）
- 反例（不提取）：「代码质量还不错」（主观评价，模糊，不提取）
- 【瞬时状态·不提取】（P1）：版本号/HEAD/commit/哈希类（「HEAD 在 1a2b3c4」「commit abc123」）、
  运行号/任务号（「run 42 失败了」「构建 #17 通过」）、完成态任务断言/任务描述
  （「已完成 X」「已交付 X」）——一次性/换 commit 即失效的信息，一律不提取；
  进行中的进度状态（「轨道B开发进行到一半」）是有长期意义的 status，照常提取。

【总原则】
- 宁缺毋滥：拿不准就不提取。污染记忆比漏记更糟。
- 只提取 AI 已经陈述为事实的稳定信息，不提取 AI 的意图、能力、承诺、客套。
- 每条记忆必须带 source_quote（AI 原话片段，直接截取，不要改写）。
- 判断该记忆关联到文本中最具体的实体（如「config.yaml」「proxy_app.py」「bug」）。

【输出格式】只输出一个紧凑 JSON 对象（不要缩进、不要多余换行）：
{"entities":[{"name":"实体名","type":"Concrete|Abstract|Event","aliases":[]}],
 "memories":[{"type":"resource|status","content":"记忆内容","entity_names":["关联实体名"],"source_quote":"AI 原话"}]}
没有就输出空数组。"""


def extract_response_items(ai_text: str) -> dict[str, Any]:
    """轨道B：从 AI 回复文本提取 resource/status（独立 prompt，宁缺毋滥）。

    返回 {"entities": [...], "memories": [...]}，与 extract() 同构，供入库复用。
    LLM 调用失败/超时抛异常（上层软失败即可）；ai_text 为空返回空结构。
    """
    if not ai_text or not ai_text.strip():
        return {"entities": [], "memories": []}
    try:
        from hippocampus.memory import security as secmod

        if secmod.should_hard_block(secmod.check_text(ai_text)):
            return {"entities": [], "memories": []}
    except Exception:
        pass
    # 动态 max_tokens：长回复防截断（复用 extract 的调参思路）
    mt = min(_MAX_OUTPUT_TOKENS, 2000 + len(ai_text) // 2)
    user = f"AI 回复文本：\n{ai_text}"
    try:
        result = chat_json(EXTRACT_RESPONSE_SYSTEM, user, max_tokens=mt)
        result["memories"] = filter_valid_memories(result.get("memories", []))
        return result
    except Exception:
        # 重试一次（max_tokens ×1.5，封顶），两次都失败才抛
        mt2 = min(_MAX_OUTPUT_TOKENS, int(mt * 1.5))
        result = chat_json(EXTRACT_RESPONSE_SYSTEM, user, max_tokens=mt2)
        result["memories"] = filter_valid_memories(result.get("memories", []))
        return result
