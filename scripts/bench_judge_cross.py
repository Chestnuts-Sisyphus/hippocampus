#!/usr/bin/env python
"""judge 一致性标定（六轮 G4/B3）：同题双判分歧率（第 1 judge vs 第 2 judge）。

为什么需要：官方分（LME 70.5% 等）的 LLM judge 是 deepseek-chat，论文基线用 GPT-4 系；
judge 模型差异是否让数字失真从未测过。本脚本对**同一批 prediction** 用第二个 judge
模型重判一遍（prompt 仍是 LME 官方逐字版），报分歧率与样例。

用法（先跑出第一份 LME 官方臂报告，再对本脚本换 env 指到第二个端点/模型）：

    # 第 1 judge（通常已含在 --model-arm 批内）：deepseek-chat
    # 第 2 judge（env 指第二个提供方；请求体与判分 prompt 完全不变）：
    HIPPOCAMPUS_BASE_URL="https://open.bigmodel.cn/api/paas/v4" \
    HIPPOCAMPUS_MODEL="glm-4-flash" \
    HIPPOCAMPUS_API_KEY="<env 注入，零字面量>" \
      python scripts/bench_judge_cross.py \
        --report D:/tmp/hc-bench/lme_official_500b.json \
        --limit 50 --out D:/tmp/hc-bench/judge_cross.json

输出：分歧率（label 不一致 / 双判题数，n 与抽样位置一起报）、≤8 条分歧样例原文
（question/gold/prediction/两个 judge 的响应）、实际调用数与花费（估算+余额差双报）。
只跑 judge 调用（~50 次），不发作答；预算硬停沿用模型臂口径。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.pop("HIPPOCAMPUS_OFFLINE", None)  # 本脚本只用于在线端点（叫 judge 需要真端点）
for _k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)  # 只认显式注入的本脚本端点

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hippocampus.eval.model_arm import (  # noqa: E402
    _CallStats,
    call_chat,
    lme_judge_prompt,
    lme_label,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LME judge 双判分歧率（同一批 prediction 换 judge 重判）")
    ap.add_argument("--report", required=True, help="LME 官方臂报告 JSON（--model-arm 输出）")
    ap.add_argument("--limit", type=int, default=50, help="双判题量（默认 50；0=全部）")
    ap.add_argument("--out", help="结果 JSON 路径")
    ap.add_argument("--budget-yuan", type=float, default=2.0, help="预算硬停（默认 ¥2）")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args(argv)

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    ret_rows = {r["qid"]: r for r in report.get("rows", [])}
    off_rows = report.get("official", {}).get("rows") or []
    if not off_rows:
        sys.exit("报告里没有 official.rows（不是 --model-arm 输出？）")
    # 只取第 1 judge 已判过（label 非 None）且没报错的题，保序取前 limit 道
    judged = [r for r in off_rows if r.get("label") is not None and not r.get("error") and not r.get("skipped")]
    batch = judged[: args.limit] if args.limit else judged
    if not batch:
        sys.exit("没有可双判的题（无 label/skip 全空？）")
    print(f"候选 {len(judged)} 题 → 双判前 {len(batch)} 题（抽样位置=报告题序前 {len(batch)} 个已判题）")

    stats = _CallStats(budget_yuan=args.budget_yuan)
    disagree: list[dict] = []
    agree_n = 0
    t0 = time.time()
    for i, row in enumerate(batch, 1):
        qid = row["qid"]
        r = ret_rows.get(qid, {})
        try:
            judge_user = lme_judge_prompt(
                str(r.get("category") or r.get("question_type") or ""),
                str(r.get("question") or ""),
                str((r.get("gold") or [""])[0] if isinstance(r.get("gold"), list) else r.get("gold") or ""),
                str(row.get("prediction") or ""),
                abstention="_abs" in qid,
            )
            out = call_chat(judge_user, max_tokens=10, kind="judge", stats=stats,
                            retries=2, timeout_s=120.0)
            label2 = lme_label(out.strip())
        except Exception as e:  # noqa: BLE001
            print(f"  {qid} judge2 失败: {str(e)[:120]}")
            continue
        label1 = bool(row.get("label"))
        if label1 != label2:
            disagree.append({
                "qid": qid,
                "question": r.get("question"),
                "gold": r.get("gold"),
                "prediction": row.get("prediction"),
                "judge1_label": label1,
                "judge1_response": row.get("judge_response"),
                "judge2_label": label2,
                "judge2_response": out.strip(),
            })
        else:
            agree_n += 1
        if i % 10 == 0:
            print(f"  …{i}/{len(batch)}，分歧 {len(disagree)}，花费 ${stats.estimated_cost():.3f}")

    total = agree_n + len(disagree)
    div = (len(disagree) / total * 100.0) if total else 0.0
    print("\n== 双判分歧率 ==")
    print(f"共 {total} 题：一致 {agree_n}，分歧 {len(disagree)} → 分歧率 {div:.1f}%"
          f"（n={total}；两个 judge 为 {os.environ.get('HIPPOCAMPUS_MODEL', '?')} vs deepseek-chat）")
    print(f"调用 {stats.judge_calls} 次，估算花费 ¥{stats.estimated_cost():.3f}，耗时 {time.time()-t0:.0f}s")
    print("\n分歧样例（≤8 条原文）：")
    for d in disagree[:8]:
        print("-" * 60)
        print(f"[{d['qid']}] Q: {str(d['question'])[:130]}")
        print(f"  GOLD: {str(d['gold'])[:120]}")
        print(f"  PRED: {str(d['prediction'])[:150]}")
        print(f"  judge1({str(bool(d['judge1_label']))}): {str(d['judge1_response'])[:120]}")
        print(f"  judge2({str(bool(d['judge2_label']))}): {str(d['judge2_response'])[:120]}")

    if args.out:
        out = {
            "divergence_pct": round(div, 2),
            "n_total": total,
            "n_agree": agree_n,
            "n_disagree": len(disagree),
            "judge2_model": os.environ.get("HIPPOCAMPUS_MODEL", "?"),
            "judge1_model": "deepseek-chat（批内）",
            "judge_prompt_source": "LME 官方逐字版（model_arm.lme_judge_prompt）",
            "calls": stats.judge_calls,
            "estimated_yuan": round(stats.estimated_cost(), 4),
            "samples": disagree[:8],
            "sampling": f"报告题序前 {len(batch)} 个已判题",
        }
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
