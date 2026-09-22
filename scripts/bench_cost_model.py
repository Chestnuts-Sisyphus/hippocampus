"""GF1：从**已有付费批次**派生「一次完整检索＋注入」的真实 token 成本表（零花费，不调端点）。

## 为什么要派生而不是重跑
评测批次已冻结（付费动作需栗子明示）。但**真实的 provider usage 已经在报告里**：
`official.calls` / `official.tokens.prompt` / `official.tokens.completion` 是 DeepSeek 返回的
`usage` 字段逐次累加出来的，不是估算。本脚本只做三件不花钱的事：

1. **读真值**：报告的 `official` 块给出全量批（n=500，作答 500 次＋判分 500 次）的
   prompt／completion 实际 token 总数；
2. **按段拆**：用仓内**同一套 prompt 模板**（`model_arm.LME_GEN_PROMPT` /
   `lme_judge_prompt`）＋报告里逐题的 `context`／`question`／`prediction`／`judge_response`，
   在本地把四段文本原样重建，量出各段长度占比，再按占比把**真值总数**拆到
   作答输入／作答输出／判分输入／判分输入之外——拆的是真值，不是凭空估一个数；
3. **算钱**：按 `model_arm.PRICE_*`（＝deepseek-flash 高峰、cache miss 的最贵口径）
   给 $/1000 次与 $/月@假定 DAU，并把现价来源 URL 一起写进产物。

> 拆分的偏差已量化：`{date}` 槽（`question_date`）不在报告里，脚本按空串渲染，
> 因此作答输入段偏小；实际值 ≈ 十几个字符。偏差方向与量级写在产物 `caveats` 里。

## 证明等级（产物里逐项带）
- `[已证明]` provider 返回的 usage 总量、调用次数、模型名、批次时间 —— 可对账；
- `[归纳待证]` 四段占比与派生出的单请求 token —— 依赖代理分词器与线性拆法；
- `[假设]` DAU、每用户每日请求数、稳定层 token 数 —— 由调用方给或按场景列。

跑法（仓规：仓外正本路径不硬编码进 tracked 文件，由调用方给）：
    python scripts/bench_cost_model.py \
        --report <仓外 lme_official_500b.json> \
        --out results/cost_retrieval_injection.json --dau 1000 --requests-per-user 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 价目来源（2026-09-22 核对；见 docs/observability-cost.md 的价目表）
PRICE_SOURCE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
PRICE_SOURCE_NOTE = (
    "deepseek-flash 高峰价（cache miss 输入 ¥2/M、输出 ¥8/M）；空闲时段为半价（¥1/M、¥4/M）；"
    "cache hit 输入 ¥0.04/M（空闲 ¥0.02/M）。取最贵口径＝宁高不低，与 model_arm.PRICE_* 一致。"
)
CACHE_HIT_PRICE_CNY_PER_M = 0.04


def _token_counter():
    """取一个**代理分词器**（不是 deepseek 的 tokenizer，产物里如实标注）。

    优先 `tiktoken` 的 `o200k_base`（能正确处理中英混排）；不可用时退回
    CJK/拉丁分档的字符估算（仓内 deprecated 档位也不影响主结论）。
    """
    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")

        def count(text: str) -> int:
            return len(enc.encode(text or ""))

        return count, "tiktoken:o200k_base（代理分词器，非 deepseek 官方 tokenizer）"
    except Exception:  # noqa: BLE001 - 缺 tiktoken 不阻断主流程
        def count(text: str) -> int:
            cjk = sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")
            return int(cjk * 0.6 + (len(text or "") - cjk) * 0.25 + 0.999)

        return count, "字符分档估算（CJK 0.6/字、拉丁 0.25/字；tiktoken 不可用时的兜底）"


def build_segments(metric_rows: list[dict[str, Any]], official_rows: list[dict[str, Any]], count) -> tuple[dict[str, int], int]:
    """逐题重建四段文本并累计代理 token 数（零出站、零花费）。

    两个 rows 是**两张表**，必须按 qid 联结：
    - 报告顶层 `rows`＝检索口径行（有 `context`／`question`／`gold`／`category`）；
    - `official.rows`＝作答/判分行（有 `prediction`／`judge_response`）。
    返回 (分段累计, 成功联结的题数)。
    """
    from hippocampus.eval.model_arm import LME_GEN_PROMPT, lme_judge_prompt

    by_qid = {r["qid"]: r for r in official_rows if r.get("qid")}
    totals = {"answer_prompt": 0, "injection": 0, "answer_completion": 0, "judge_prompt": 0, "judge_completion": 0}
    joined = 0
    for row in metric_rows:
        official_row = by_qid.get(row.get("qid"), {})
        if official_row:
            joined += 1
        context = row.get("context") or ""
        question = row.get("question") or ""
        gold = (row.get("gold") or [""])[0]
        prediction = official_row.get("prediction") or ""
        segments = {
            # {date} 槽（question_date）不在任何一张表里 → 空串渲染（偏差见模块 docstring）
            "answer_prompt": LME_GEN_PROMPT.format(history=context, date="", question=question),
            "injection": context,
            "answer_completion": prediction,
            "judge_prompt": lme_judge_prompt(
                row.get("category") or "", question, gold, prediction, abstention="_abs" in (row.get("qid") or "")
            ),
            "judge_completion": official_row.get("judge_response") or "",
        }
        for key, text in segments.items():
            totals[key] += count(text)
    return totals, joined


def cost_cny(tokens: float, per_m: float) -> float:
    return tokens / 1e6 * per_m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="派生一次检索＋注入的真实 token 成本表（零花费）")
    parser.add_argument("--report", required=True, help="仓外官方判分批报告 JSON（含 official.rows）")
    parser.add_argument("--out", default=str(ROOT / "results" / "cost_retrieval_injection.json"))
    parser.add_argument("--dau", type=int, default=1000, help="[假设] 日活用户数")
    parser.add_argument("--requests-per-user", type=int, default=10, help="[假设] 每用户每日请求数")
    parser.add_argument(
        "--stable-tokens", type=int, nargs="*", default=[200, 500, 1000], help="[假设] 稳定层 token 数场景（缓存收益）"
    )
    args = parser.parse_args(argv)

    from hippocampus.eval.model_arm import PRICE_INPUT_CNY_PER_M, PRICE_OUTPUT_CNY_PER_M

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    official = report["official"]
    rows = report["rows"]
    real_prompt = int(official["tokens"]["prompt"])
    real_completion = int(official["tokens"]["completion"])
    n_answer = int(official["calls"]["answer"])
    n_judge = int(official["calls"]["judge"])

    count, counter_note = _token_counter()
    proxy, joined = build_segments(rows, official.get("rows") or [], count)
    proxy_prompt_total = proxy["answer_prompt"] + proxy["judge_prompt"]
    proxy_completion_total = proxy["answer_completion"] + proxy["judge_completion"]

    # 按占比把**真值**拆到各段（比例来自本地重建，量纲来自 provider 实测）
    k_prompt = real_prompt / proxy_prompt_total if proxy_prompt_total else 0.0
    k_completion = real_completion / proxy_completion_total if proxy_completion_total else 0.0
    seg = {
        "answer_prompt": proxy["answer_prompt"] * k_prompt,
        "judge_prompt": proxy["judge_prompt"] * k_prompt,
        "injection": proxy["injection"] * k_prompt,
        "answer_completion": proxy["answer_completion"] * k_completion,
        "judge_completion": proxy["judge_completion"] * k_completion,
    }

    per_answer_in = seg["answer_prompt"] / n_answer
    per_answer_out = seg["answer_completion"] / n_answer
    per_judge_in = seg["judge_prompt"] / n_judge
    per_judge_out = seg["judge_completion"] / n_judge
    injection_share = seg["injection"] / seg["answer_prompt"] if seg["answer_prompt"] else 0.0

    # 生产口径＝一次检索＋注入＋作答（不含判分；判分只在评测里出现）
    cost_per_request = cost_cny(per_answer_in, PRICE_INPUT_CNY_PER_M) + cost_cny(per_answer_out, PRICE_OUTPUT_CNY_PER_M)
    cost_per_1k = cost_per_request * 1000
    monthly_requests = args.dau * args.requests_per_user * 30
    cost_per_month = cost_per_request * monthly_requests
    # 记忆层自身那份（注入段）单独算：回答"你的记忆系统花了多少钱"
    cost_injection_1k = cost_cny(per_answer_in * injection_share, PRICE_INPUT_CNY_PER_M) * 1000

    cache_scenarios = []
    for stable_tokens in args.stable_tokens:
        full = cost_cny(stable_tokens, PRICE_INPUT_CNY_PER_M)
        cached = cost_cny(stable_tokens, CACHE_HIT_PRICE_CNY_PER_M)
        cache_scenarios.append(
            {
                "stable_tokens": stable_tokens,
                "no_cache_cny_per_request": round(full, 8),
                "cached_cny_per_request": round(cached, 8),
                "saving_cny_per_1k": round((full - cached) * 1000, 4),
                "saving_cny_per_month": round((full - cached) * monthly_requests, 4),
                "level": "[假设] 稳定层 token 数为场景参数，未实测",
            }
        )

    payload = {
        "generated_from": {
            "report_model": official.get("model"),
            "report_n": official.get("n"),
            "report_calls": official.get("calls"),
            "report_tokens": official.get("tokens"),
            "report_cost_est_yuan": official.get("cost_est_yuan"),
            "real_spend_yuan": official.get("spend_yuan"),
            "level": "[已证明] provider 返回的 usage 累加，可对账",
        },
        "counter": counter_note,
        "rows_joined_by_qid": joined,
        "n_metric_rows": len(rows),
        "split": {
            "k_prompt": round(k_prompt, 4),
            "k_completion": round(k_completion, 4),
            "note": "真值总量按本地重建文本的占比线性拆到各段；{date} 槽缺失使作答输入段偏小",
            "level": "[归纳待证]",
        },
        "per_request_production": {
            "input_tokens": round(per_answer_in, 2),
            "output_tokens": round(per_answer_out, 2),
            "injection_input_tokens": round(per_answer_in * injection_share, 2),
            "injection_share_of_input": round(injection_share, 4),
            "level": "[归纳待证]",
        },
        "per_request_judge": {
            "input_tokens": round(per_judge_in, 2),
            "output_tokens": round(per_judge_out, 2),
            "note": "判分臂只在评测批次出现，不进生产请求成本",
            "level": "[归纳待证]",
        },
        "price": {
            "source_url": PRICE_SOURCE_URL,
            "note": PRICE_SOURCE_NOTE,
            "input_cny_per_m": PRICE_INPUT_CNY_PER_M,
            "output_cny_per_m": PRICE_OUTPUT_CNY_PER_M,
            "cache_hit_input_cny_per_m": CACHE_HIT_PRICE_CNY_PER_M,
            "checked_on": "2026-09-22",
            "level": "[已证明] 现价页面逐档核对；model_arm.PRICE_* 与之取同一（最贵）档",
        },
        "cost": {
            "cny_per_request": round(cost_per_request, 8),
            "cny_per_1000_requests": round(cost_per_1k, 4),
            "cny_per_1000_injection_only": round(cost_injection_1k, 4),
            "assumptions": {"dau": args.dau, "requests_per_user_per_day": args.requests_per_user, "days": 30},
            "monthly_requests": monthly_requests,
            "cny_per_month": round(cost_per_month, 2),
            "level": "[已证明] 单价×token；[假设] DAU 与每用户请求数为参数",
        },
        "cache_scenarios": cache_scenarios,
        "caveats": [
            "拆分为线性比例法：真值总量是 provider 实测，段间占比来自本地重建（代理分词器）。",
            "作答 prompt 的 {date} 槽（question_date）不在报告里，按空串渲染 → 作答输入段偏小十几个字符。",
            "deepseek-chat 在 2026-09-22 的现价页上已不单列（页面现列 deepseek-flash／deepseek-v4-pro），"
            "批次当时用的是 deepseek-chat；本表按现价页的 flash 档计价，若模型映射变化需重算。",
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"产物：{out_path}")
    print(f"真值（provider）：prompt={real_prompt} / completion={real_completion}，作答 {n_answer} 次＋判分 {n_judge} 次")
    print(f"分词口径：{counter_note}    拆分系数 k_prompt={k_prompt:.3f} k_completion={k_completion:.3f}")
    print("一次完整检索＋注入（生产臂，不含判分）：")
    print(f"  输入 {per_answer_in:.1f} tok（其中注入段 {per_answer_in * injection_share:.1f} tok = {injection_share:.0%}）")
    print(f"  输出 {per_answer_out:.1f} tok")
    print(f"  ¥{cost_per_request:.6f}/次   ¥{cost_per_1k:.2f}/1000 次   ¥{cost_per_month:.2f}/月@DAU={args.dau}×{args.requests_per_user}次")
    print(f"  只算记忆注入那一段：¥{cost_injection_1k:.2f}/1000 次")
    print("稳定层缓存敏感性（[假设] 稳定层 token 数）：")
    for s in cache_scenarios:
        print(
            f"  {s['stable_tokens']:>5} tok：不缓存 ¥{s['no_cache_cny_per_request']:.6f}/次 → 缓存后 "
            f"¥{s['cached_cny_per_request']:.6f}/次，省 ¥{s['saving_cny_per_1k']:.2f}/1000 次、"
            f"¥{s['saving_cny_per_month']:.2f}/月"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
