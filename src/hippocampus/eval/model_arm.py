"""官方判分臂（模型作答 + LLM 判分）——显式开关才出站，离线默认行为零变化。

## 为什么有本模块
`bench …` 离线档只给"检索/词面"口径（证据命中、词面 F1）。两个公认基准的
**官方口径**要两步：大模型基于注入上下文作答 +（LongMemEval 还要）LLM 判分。
本模块补这两步。prompt 与判分算法**逐一对应官方仓库**（出处与钉死的 revision
见下方 `SOURCE_*` 常量），与官方唯一的输入差异是：**作答输入 = 记忆层选出的
注入上下文（top-8，`inject_finalize` 原文），不是全文**——这正是要测的东西
（记忆系统选得对不对），protocol 里写明，对照表里每行带口径脚注。

## 官方出处（钉 revision，clone 本机 D:/tmp/hc-bench/ 可复核）
- LoCoMo（ACL'24）：`snap-research/locomo` @ `3eb6f2c`
  - 作答 prompt：`task_eval/gpt_utils.py`（QA_PROMPT／cat2 日期后缀／cat5 选项题）
  - 判分：`task_eval/evaluation.py` `eval_question_answering`——
    cat1 拆子答案取 max、cat2/3/4 走 Porter 词干词面 F1（normalize 去 a/an/the/and）、
    cat5 判输出含 "not mentioned/not available"；总体＝每题分数均值
- LongMemEval（ICLR'25）：`xiaowu0162/LongMemEval` @ `9e0b455`
  - 作答 prompt：`src/generation/run_generation.py`（history_format=full；temperature 0、max_tokens 500）
  - 判分 prompt：`src/evaluation/evaluate_qa.py` `get_anscheck_prompt`
    （按题型模板；question_id 含 `_abs` 走 abstention 模板；judge 输出 yes/no，
    `'yes' in response.lower()`；temperature 0、max_tokens 10）

## 护栏（调用链稳定性）
- 并发 16、失败重试 2 次、单调用超时 120 s；
- 跑前查余额（GET https://api.deepseek.com/user/balance，零成本，URL 过出站校验），跑后复查；
- 按 usage 估算花费（价目常量见 `PRICES_*`，仅估算；实际花费＝余额差）；
  累计估算 ≥ 预算（默认 ¥30）→ 硬停，未跑题记 skipped，不许缩口径冒充全量；
- 凭据零字面量：只走 `config.resolve_api_key`（环境变量／密钥服务），不打印、不落盘。
"""

from __future__ import annotations

import statistics  # noqa: F401 - 保留（后续统计用）；ruff 需要时移除
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from hippocampus.memory.config import get_llm_config
from hippocampus.net import UnsafeURLError, validate_outbound_url

# ----------------------------------------------------------------------
# 官方出处（钉 revision）
# ----------------------------------------------------------------------

SOURCE_LOCoMo = "snap-research/locomo @3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376（ACL'24）"
SOURCE_LME = "xiaowu0162/LongMemEval @9e0b455f4ef0e2ab8f2e582289761153549043fc（ICLR'25）"

# ----------------------------------------------------------------------
# 官方 prompt（照抄官方仓库，仅填空）
# ----------------------------------------------------------------------

# LoCoMo task_eval/gpt_utils.py
LOCOMO_QA_PROMPT = (
    "Based on the above context, write an answer in the form of a short phrase for the following question. "
    "Answer with exact words from the context whenever possible.\n\nQuestion: {question} Short answer:"
)
LOCOMO_CAT2_SUFFIX = " Use DATE of CONVERSATION to answer with an approximate date."
LOCOMO_CAT5_PROMPT = "Based on the above context, answer the following question.\n\nQuestion: {question} Short answer:"

# LongMemEval src/generation/run_generation.py（history_format='full'，把官方 history 换成我们的注入上下文）
LME_GEN_PROMPT = (
    "I will give you several history chats between you and a user. Please answer the question based on the "
    "relevant chat history.\n\n\nHistory Chats:\n\n{history}\n\nCurrent Date: {date}\nQuestion: {question}\nAnswer:"
)

# LongMemEval src/evaluation/evaluate_qa.py get_anscheck_prompt（逐题型模板原文）
_LME_JUDGE_TEMPLATES: dict[str, str] = {
    "short": (
        "I will give you a question, a correct answer, and a response from a model. Please answer yes if "
        "the response contains the correct answer. Otherwise, answer no. If the response is equivalent to "
        "the correct answer or contains all the intermediate steps to get the correct answer, you should "
        "also answer yes. If the response only contains a subset of the information required by the answer, "
        "answer no. \n\nQuestion: {question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "temporal-reasoning": (
        "I will give you a question, a correct answer, and a response from a model. Please answer yes if "
        "the response contains the correct answer. Otherwise, answer no. If the response is equivalent to "
        "the correct answer or contains all the intermediate steps to get the correct answer, you should "
        "also answer yes. If the response only contains a subset of the information required by the answer, "
        "answer no. In addition, do not penalize off-by-one errors for the number of days. If the question "
        "asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., "
        "predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: "
        "{question}\n\nCorrect Answer: {answer}\n\nModel Response: {response}\n\n"
        "Is the model response correct? Answer yes or no only."
    ),
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. Please answer yes if "
        "the response contains the correct answer. Otherwise, answer no. If the response contains some "
        "previous information along with an updated answer, the response should be considered as correct "
        "as long as the updated answer is the required answer.\n\nQuestion: {question}\n\nCorrect Answer: "
        "{answer}\n\nModel Response: {response}\n\nIs the model response correct? Answer yes or no only."
    ),
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and a response from a "
        "model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
        "The model does not need to reflect all the points in the rubric. The response is correct as long "
        "as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {question}"
        "\n\nRubric: {answer}\n\nModel Response: {response}\n\nIs the model response correct? "
        "Answer yes or no only."
    ),
}
_LME_JUDGE_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. Please answer "
    "yes if the model correctly identifies the question as unanswerable. The model could say that the "
    "information is incomplete, or some other information is given but the asked information is not."
    "\n\nQuestion: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer yes or no only."
)

# ----------------------------------------------------------------------
# 价目（估算用；实际花费以余额差为准）
# ----------------------------------------------------------------------

# deepseek-chat 官方价目（api-docs.deepseek.com/quick_start/pricing，2026-09 折算 CNY：
# 输入 cache miss ¥2/M、输出 ¥8/M；cache hit 更低，此处按最贵口径估，宁高不低）
PRICE_INPUT_CNY_PER_M = 2.0
PRICE_OUTPUT_CNY_PER_M = 8.0

# 余额查询端点（零成本；出站前过 validate_outbound_url）
BALANCE_URL = "https://api.deepseek.com/user/balance"

# 默认护栏参数
DEFAULT_CONCURRENCY = 16
DEFAULT_RETRIES = 2
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_BUDGET_YUAN = 30.0


class _Lcg:
    """评测复现用的确定性伪随机数生成器（**非加密用途**：只用于 cat5 选项换位与
    自助法重抽样，官方仓库同样用 `random` 换选项位——这里换成固定种子的 LCG，
    保证跨进程/跨机器可复跑，且不引入密码学随机性的误用暗示）。"""

    def __init__(self, seed: int) -> None:
        self._state = (seed & 0xFFFFFFFF) or 1
        self._big = 0xFFFFFFFFFFFFFFFF

    def next(self) -> int:
        self._state = (self._state * 6364136223846793005 + 1442695040888963407) & self._big
        return self._state

    def random(self) -> float:
        return (self.next() >> 11) / (1 << 53)

    def choice(self, seq: list[Any]) -> Any:
        return seq[int(self.random() * len(seq))] if seq else None


# ----------------------------------------------------------------------
# 统计工具
# ----------------------------------------------------------------------


def wilson_ci(n: int, hits: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间（二项比例，LongMemEval 准确率用；n=0 返回 (0,0)）。"""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_ci(values: list[float], seed: int = 42, n_resample: int = 2000) -> tuple[float, float]:
    """百分位自助法 95% CI（连续分值，LoCoMo F1 用；固定种子可复跑）。"""
    if not values:
        return (0.0, 0.0)
    rng = _Lcg(seed)
    means: list[float] = []
    for _ in range(n_resample):
        sample = [rng.choice(values) for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.025 * (n_resample - 1))]
    hi = means[int(0.975 * (n_resample - 1))]
    return (lo, hi)


# ----------------------------------------------------------------------
# LoCoMo 官方判分（port of locomo task_eval/evaluation.py）
# ----------------------------------------------------------------------


def loco_normalize_answer(text: str) -> str:
    """官方 normalize_answer：lower → **删除** string.punctuation（join，非替换）→
    去 a/an/the/and（\\b 词边界）→ 压空白。与官方 evaluation.py 逐字一致。"""
    import re as _re
    import string as _string

    t = (text or "").lower()
    t = t.replace(",", "")  # 官方第 0 步（与 remove_punc 重复，照抄顺序）
    t = "".join(ch for ch in t if ch not in set(_string.punctuation))
    t = _re.sub(r"\b(a|an|the|and)\b", " ", t)
    return " ".join(t.split())


def loco_f1_score(prediction: str, ground_truth: str) -> float:
    """官方 f1_score：Porter 词干化后的词面 F1（与官方 evaluation.py 同款）。"""
    from nltk.stem import PorterStemmer

    stemmer = PorterStemmer()
    p_toks = [stemmer.stem(w) for w in loco_normalize_answer(prediction).split()]
    g_toks = [stemmer.stem(w) for w in loco_normalize_answer(ground_truth).split()]
    common = Counter(p_toks) & Counter(g_toks)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(p_toks)
    recall = num_same / len(g_toks)
    return 2 * precision * recall / (precision + recall)


def loco_f1_multi(prediction: str, ground_truth: str) -> float:
    """官方 f1：答案按逗号拆成多个子答案，两两取 max 后平均（cat1 多跳用）。"""
    predictions = [p.strip() for p in prediction.split(",") if p.strip()]
    ground_truths = [g.strip() for g in ground_truth.split(",") if g.strip()]
    if not predictions or not ground_truths:
        return 0.0
    scores = []
    for gt in ground_truths:
        scores.append(max(loco_f1_score(p, gt) for p in predictions))
    return sum(scores) / len(scores)


def loco_cat5_correct(output: str) -> float:
    """官方 cat5（对抗题）判据：输出含 'no information available' 或 'not mentioned' → 1。"""
    lower = (output or "").lower()
    return 1.0 if ("no information available" in lower or "not mentioned" in lower) else 0.0


def loco_get_cat5_answer(model_output: str, answer_key: dict[str, str]) -> str:
    """官方 get_cat_5_answer：单字母/带括号字母输出映射回选项文本。"""
    text = (model_output or "").strip().lower()
    if len(text) == 1:
        return answer_key.get("a" if "a" in text else "b", model_output)
    if len(text) == 3:
        if "(a)" in text:
            return answer_key["a"]
        return answer_key["b"]
    return model_output


def loco_question_prompt(category: int, question: str, gold: str, rng: _Lcg) -> tuple[str, dict[str, str]]:
    """官方作答 prompt（gpt_utils.py）：cat2 加日期后缀；cat5 变成 (a)/(b) 选项题（随机换位）。"""
    if category == 2:
        return LOCOMO_QA_PROMPT.format(question=question + LOCOMO_CAT2_SUFFIX), {}
    if category == 5:
        options = ["Not mentioned in the conversation", gold]
        if rng.random() < 0.5:
            options = options[::-1]
        answer_key = {"a": options[0], "b": options[1]}
        return LOCOMO_CAT5_PROMPT.format(
            question=question + f" Select the correct answer: (a) {options[0]} (b) {options[1]}."
        ), answer_key
    return LOCOMO_QA_PROMPT.format(question=question), {}


def loco_score_one(category: int, prediction: str, gold: str) -> float:
    """官方 eval_question_answering 的单题分（cat 分支与官方一致）。"""
    if category == 3:  # 开放域：参考答案按 ';' 拆取第一段
        gold = gold.split(";")[0].strip()
    if category in (2, 3, 4):
        return loco_f1_score(prediction, gold)
    if category == 1:
        return loco_f1_multi(prediction, gold)
    if category == 5:
        return loco_cat5_correct(prediction)
    raise ValueError(f"未知 LoCoMo 类别: {category}")


# ----------------------------------------------------------------------
# LongMemEval 判分 prompt / 标签
# ----------------------------------------------------------------------


def lme_judge_prompt(question_type: str, question: str, answer: str, response: str, abstention: bool) -> str:
    """官方 get_anscheck_prompt（含 per-type 模板与 _abs abstention 模板）。"""
    if abstention:
        return _LME_JUDGE_ABSTENTION.format(question=question, answer=answer, response=response)
    template = _LME_JUDGE_TEMPLATES.get(question_type) or _LME_JUDGE_TEMPLATES["short"]
    return template.format(question=question, answer=answer, response=response)


def lme_label(response: str) -> bool:
    """官方判分标签：'yes' in response.lower()。"""
    return "yes" in (response or "").lower()


# ----------------------------------------------------------------------
# 调用链（护栏：并发 / 重试 / 超时 / 预算硬停 / 计数与估算）
# ----------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    """累计估算花费 ≥ 预算：硬停，未跑题记 skipped。"""


class _CallStats:
    """线程安全调用统计：次数、usage 汇总、失败、预算态。"""

    def __init__(self, budget_yuan: float) -> None:
        self.lock = threading.Lock()
        self.answer_calls = 0
        self.judge_calls = 0
        self.failures: list[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.budget_yuan = budget_yuan

    def estimated_cost(self) -> float:
        return (self.prompt_tokens / 1e6) * PRICE_INPUT_CNY_PER_M + (
            self.completion_tokens / 1e6
        ) * PRICE_OUTPUT_CNY_PER_M

    def check_budget(self) -> None:
        if self.estimated_cost() >= self.budget_yuan:
            raise BudgetExceeded(f"累计估算花费 ¥{self.estimated_cost():.2f} 已达预算 ¥{self.budget_yuan:.2f}，硬停")


def call_chat(
    user: str,
    *,
    max_tokens: int,
    kind: str,
    stats: _CallStats,
    retries: int = DEFAULT_RETRIES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str:
    """单次模型调用（重试后仍失败抛异常）。凭据只经 get_llm_config 环境变量通道。"""
    cfg = get_llm_config()
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    from hippocampus.memory import llm

    last_err = ""
    for attempt in range(retries + 1):
        with stats.lock:
            stats.check_budget()
            if kind == "answer":
                stats.answer_calls += 1
            else:
                stats.judge_calls += 1
        try:
            status, body = llm.post_json(payload, timeout_s=timeout_s)
            if status != 200:
                raise RuntimeError(f"上游 {status}: {str(body)[:200]}")
            content = body["choices"][0]["message"].get("content", "")
            usage = body.get("usage") or {}
            with stats.lock:
                stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                stats.completion_tokens += int(usage.get("completion_tokens") or 0)
            return content
        except (httpx.HTTPError, KeyError, ValueError, RuntimeError, TypeError) as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    with stats.lock:
        stats.failures.append(f"{kind}={last_err}")
    raise RuntimeError(f"模型调用失败（已重试 {retries} 次）：{last_err}"[:400])


def fetch_balance_cny() -> float | None:
    """查询 DeepSeek 余额（CNY）。出站 URL 先过 validate_outbound_url（拒环回/私有/保留）；失败返回 None。"""
    cfg = get_llm_config()
    try:
        url = validate_outbound_url(BALANCE_URL)
        headers = {"Authorization": f"Bearer {cfg['api_key']}"}
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        for info in data.get("balance_infos") or []:
            if str(info.get("currency") or "").upper() == "CNY":
                return float(info.get("total_balance") or 0.0)
        return None
    except (UnsafeURLError, httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def _print_progress(stats: _CallStats, every: int = 25) -> None:
    with stats.lock:
        total = stats.answer_calls + stats.judge_calls
        cost = stats.estimated_cost()
    if total % every == 0:
        print(
            f"  已调用 {total} 次（作答 {stats.answer_calls} / 判分 {stats.judge_calls}），"
            f"估算花费 ¥{cost:.3f}（预算 ¥{stats.budget_yuan:.2f}）"
        )


@dataclass
class ArmItem:
    """模型臂的一道题：id/题型/问题/参考答案/注入上下文/判分所需额外字段。"""

    qid: str
    category: str = ""  # LoCoMo: catN；LME: question_type
    question: str = ""
    answers: list[str] = field(default_factory=list)
    context: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # question_date / cat5 answer_key 等


@dataclass
class ArmResult:
    """一道题的模型臂结果。"""

    qid: str
    prediction: str = ""
    judge_response: str = ""
    label: bool | None = None
    score: float | None = None  # 官方单题分（LoCoMo F1／cat5 0-1；LME 1/0）
    error: str = ""
    skipped: bool = False


def run_official(
    bench: str,
    items: list[ArmItem],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    budget_yuan: float = DEFAULT_BUDGET_YUAN,
    cat5_seed: int = 1234,
    answer_max_tokens: int = 500,
) -> dict[str, Any]:
    """跑官方判分臂（模型作答 + LME 判分），返回报告 dict。

    - 并发 `concurrency`；每题一个 worker（LME 的作答与判分在同一 worker 内串行，
      保证 judge 的输入是它自己那题的回答）；
    - 预算硬停：累计**估算**花费 ≥ `budget_yuan` 时不再发起新调用（in-flight 跑完）；
    - 跑前/跑后各查一次余额（零成本），实际花费＝余额差；查询失败如实记录；
    - 任一题调用失败（重试后）记 error；**有失败就如实报**，不许缩口径冒充全量；
    - `answer_max_tokens` 默认 500＝官方 LME 生成 `gen_length`（LoCoMo 作答固定 32＝官方
      `num_tokens_request`）；LME 判分固定 max_tokens 10（官方值）。
    """
    stats = _CallStats(budget_yuan=budget_yuan)
    balance_before = fetch_balance_cny()
    if balance_before is not None:
        print(f"  余额（跑前）：¥{balance_before:.2f}")
    else:
        print("  余额（跑前）：查询失败")
    rng = _Lcg(cat5_seed)
    results: list[ArmResult | None] = [None] * len(items)

    def process(idx: int, item: ArmItem) -> None:
        res = ArmResult(qid=item.qid)
        results[idx] = res
        try:
            if bench in ("locomo", "locomo10"):
                cat = int(str(item.category).replace("cat", "") or 0)
                # cat5 对抗题：数据字段是 adversarial_answer（官方代码引 qa['answer'] 处对应的就是它）
                gold = str(item.extra.get("adversarial_answer") or item.answers[0] or "")
                prompt, answer_key = loco_question_prompt(cat, item.question, gold, rng)
                user = item.context + "\n\n" + prompt
                output = call_chat(
                    user, max_tokens=32, kind="answer", stats=stats, retries=retries, timeout_s=timeout_s
                )
                if cat == 5 and answer_key:
                    output = loco_get_cat5_answer(output, answer_key)
                res.prediction = output.strip()
                res.score = loco_score_one(cat, res.prediction, gold)
            else:  # longmemeval / lme
                question_date = str(item.extra.get("question_date") or "")
                user = LME_GEN_PROMPT.format(history=item.context, date=question_date, question=item.question)
                output = call_chat(
                    user, max_tokens=answer_max_tokens, kind="answer", stats=stats, retries=retries, timeout_s=timeout_s
                )
                res.prediction = output.strip()
                abstention = "_abs" in item.qid
                judge_user = lme_judge_prompt(
                    item.category, item.question, item.answers[0], res.prediction, abstention=abstention
                )
                judge_out = call_chat(
                    judge_user, max_tokens=10, kind="judge", stats=stats, retries=retries, timeout_s=timeout_s
                )
                res.judge_response = judge_out.strip()
                res.label = lme_label(judge_out)
                res.score = 1.0 if res.label else 0.0
            _print_progress(stats)
        except BudgetExceeded as e:
            res.skipped = True
            res.error = str(e)
        except Exception as e:  # noqa: BLE001 - 逐题失败记下来，不让一个脏数据杀掉整批
            res.error = str(e)[:300]
            with stats.lock:
                stats.failures.append(f"{item.qid}={e}"[:200])

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(process, i, it): i for i, it in enumerate(items)}
        for fut in as_completed(futures):
            fut.result()  # process 内部已捕获，这里只需等它完成

    balance_after = fetch_balance_cny()
    if balance_after is not None:
        print(f"  余额（跑后）：¥{balance_after:.2f}")
    else:
        print("  余额（跑后）：查询失败")

    done = [r for r in results if r is not None and not r.skipped and not r.error]
    skipped = [r for r in results if r is not None and r.skipped]
    failed = [r for r in results if r is not None and r.error and not r.skipped]
    scores = [r.score for r in done if r.score is not None]
    spent = None
    if balance_before is not None and balance_after is not None:
        spent = max(0.0, round(balance_before - balance_after, 4))
    report: dict[str, Any] = {
        "enabled": True,
        "model": get_llm_config()["model"],
        "temperature": 0,
        "concurrency": concurrency,
        "retries": retries,
        "timeout_s": timeout_s,
        "budget_yuan": budget_yuan,
        "input_spec": "记忆层注入上下文（top-8，inject_finalize 原文）；不是全文",
        "calls": {"answer": stats.answer_calls, "judge": stats.judge_calls},
        "tokens": {"prompt": stats.prompt_tokens, "completion": stats.completion_tokens},
        "cost_est_yuan": round(stats.estimated_cost(), 4),
        "spend_yuan": spent,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "n_items": len(items),
        "n_done": len(done),
        "n_failed": len(failed),
        "n_skipped": len(skipped),
        "failures": stats.failures[:20],
        "rows": [
            {
                "qid": r.qid,
                "prediction": r.prediction,
                "judge_response": r.judge_response,
                "label": r.label,
                "score": r.score,
                "error": r.error,
                "skipped": r.skipped,
            }
            for r in results
            if r is not None
        ],
    }
    if bench in ("locomo", "locomo10"):
        cat_scores: dict[str, list[float]] = {}
        for item, r in zip(items, results, strict=False):
            if r is None or r.score is None or r.error or r.skipped:
                continue
            cat_scores.setdefault(item.category, []).append(r.score)
        report["official"] = {
            "metric": "官方 F1（Porter 词干词面，cat1 拆子答案 max，cat5 拒答判据）",
            "n": len(scores),
            "f1": round(sum(scores) / len(scores), 4) if scores else None,
            "ci95": [round(v, 4) for v in bootstrap_ci(scores)],
            "by_category": {
                k: {"n": len(v), "score": round(sum(v) / len(v), 4)} for k, v in sorted(cat_scores.items())
            },
        }
    else:
        labeled = [r for r in done if r.label is not None]
        acc = sum(1 for r in labeled if r.label) / len(labeled) if labeled else None
        cat_label: dict[str, list[bool]] = {}
        for item, r in zip(items, results, strict=False):
            if r is None or r.label is None or r.error or r.skipped:
                continue
            cat_label.setdefault(item.category, []).append(r.label)
        report["official"] = {
            "metric": "官方 LLM 判分准确率（judge yes/no，官方 prompt 逐题型）",
            "n": len(labeled),
            "accuracy": round(acc, 4) if acc is not None else None,
            "ci95": [round(v, 4) for v in wilson_ci(len(labeled), sum(1 for r in labeled if r.label))],
            "by_category": {
                k: {"n": len(v), "accuracy": round(sum(1 for x in v if x) / len(v), 4)}
                for k, v in sorted(cat_label.items())
            },
        }
    return report


__all__ = [
    "ArmItem",
    "ArmResult",
    "BALANCE_URL",
    "BudgetExceeded",
    "DEFAULT_BUDGET_YUAN",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "LME_GEN_PROMPT",
    "PRICE_INPUT_CNY_PER_M",
    "PRICE_OUTPUT_CNY_PER_M",
    "SOURCE_LME",
    "SOURCE_LOCoMo",
    "bootstrap_ci",
    "call_chat",
    "fetch_balance_cny",
    "lme_judge_prompt",
    "lme_label",
    "loco_cat5_correct",
    "loco_f1_multi",
    "loco_f1_score",
    "loco_get_cat5_answer",
    "loco_normalize_answer",
    "loco_question_prompt",
    "loco_score_one",
    "run_official",
    "wilson_ci",
]
