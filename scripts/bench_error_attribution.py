#!/usr/bin/env python
"""官方分错误归因（T11/C2·C3）：官方判分批的三分类计数 + 短板类原文抽样。

回答"100% 证据命中 → 70.5% 官方分，29.5pp 丢在哪"：
1. **证据没进**：evidence_in_context=False 的题（理论上 LME 官方批为 0）；
2. **进了但答错**：证据在但 score=0；
3. **judge 可疑**：score=0 且预测文本与参考答案有明显词面重合（Porter 词干级），
   judge 却说不对——这是"可疑误判"的**启发式筛选**（真实确认需同题双判，未花钱跑）。

用法（离线纯分析，0 花费；数据来自 `--model-arm --json` 输出）：
    python scripts/bench_error_attribution.py \
        --lme D:/tmp/hc-bench/lme_official_full.json \
        --loco D:/tmp/hc-bench/loco_official_full.json
输出：三分类计数 + 每类 ≤10 条原文抽样；LoCoMo cat2 与 LME multi-session 各 10 题原文。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _stem_overlap(pred: str, gold: str) -> float:
    """词面重合度（简化 Porter：小写+去后缀 s/es/ed/ing；够用于"显然重了"的启发式）。"""
    import re

    def toks(t: str) -> set[str]:
        words = re.findall(r"[a-z]+", (t or "").lower())
        out = set()
        for w in words:
            for suf in ("ing", "es", "ed", "s"):
                if len(w) > 4 and w.endswith(suf):
                    w = w[: -len(suf)]
                    break
            out.add(w)
        return out

    p, g = toks(pred), toks(gold)
    if not g:
        return 0.0
    return len(p & g) / len(g)


def _lme_attribution(rows_main: list[dict], rows_off: list[dict], limit: int = 10) -> dict:
    by_qid = {r["qid"]: r for r in rows_main}
    miss, wrong, judge_susp = [], [], []
    for off in rows_off:
        main = by_qid.get(off["qid"], {})
        evidence_hit = bool(main.get("evidence_in_context"))
        score = float(off.get("score") or 0.0)
        judge = str(off.get("judge_response") or "").lower()
        if not evidence_hit:
            miss.append(off)
        elif score >= 1.0:
            continue
        else:
            gold = " ".join(str(a) for a in (main.get("gold") or []) if str(a).strip())
            overlap = _stem_overlap(str(off.get("prediction") or ""), gold)
            if judge == "no" and overlap >= 0.5:
                judge_susp.append(off)
            else:
                wrong.append(off)
    return {
        "n": len(rows_off),
        "evidence_miss": len(miss),
        "evidence_in_but_wrong": len(wrong),
        "judge_suspected": len(judge_susp),
        "judge_suspected_share": round(100.0 * len(judge_susp) / max(1, len(rows_off)), 2),
        "samples": {
            "evidence_miss": [{"qid": r["qid"], "pred": str(r.get("prediction") or "")[:200]} for r in miss[:limit]],
            "wrong": [
                {"qid": r["qid"], "pred": str(r.get("prediction") or "")[:200], "score": r.get("score")}
                for r in wrong[:limit]
            ],
            "judge_suspected": [
                {"qid": r["qid"], "pred": str(r.get("prediction") or "")[:200], "judge": r.get("judge_response")}
                for r in judge_susp[:limit]
            ],
        },
    }


def _loco_cat2_samples(rows_main: list[dict], rows_off: list[dict], limit: int = 10) -> list[dict]:
    by_qid = {r["qid"]: r for r in rows_main}
    out = []
    for off in rows_off:
        main = by_qid.get(off["qid"], {})
        if main.get("category") != "cat2":
            continue
        if len(out) >= limit:
            break
        out.append(
            {
                "qid": off["qid"],
                "gold": " ".join(str(a) for a in (main.get("gold") or []) if str(a).strip())[:160],
                "pred": str(off.get("prediction") or "")[:160],
                "score": round(float(off.get("score") or 0.0), 3),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="官方分错误归因（T11，0 花费纯分析）")
    ap.add_argument("--lme", required=True, help="LME 官方批 JSON（--model-arm 输出）")
    ap.add_argument("--loco", required=True, help="LoCoMo 官方批 JSON")
    ap.add_argument("--out", default="", help="结果写入路径（可选）")
    args = ap.parse_args(argv)

    lme = json.load(open(args.lme, encoding="utf-8"))
    loco = json.load(open(args.loco, encoding="utf-8"))

    lme_attr = _lme_attribution(lme["rows"], lme["official"]["rows"])
    print("== LME-oracle（200 题，官方 judge 口径）三分类 ==")
    print(f"  题量 {lme_attr['n']}：证据没进 {lme_attr['evidence_miss']}；"
          f"进了但答错 {lme_attr['evidence_in_but_wrong']}；judge 可疑 {lme_attr['judge_suspected']}"
          f"（占 {lme_attr['judge_suspected_share']}%）")
    for key in ("evidence_miss", "wrong", "judge_suspected"):
        print(f"  -- {key} 样例（≤10）--")
        for s in lme_attr["samples"][key]:
            print(f"     {s['qid']}: {s.get('pred', '')[:80]}")

    cat2 = _loco_cat2_samples(loco["rows"], loco["official"]["rows"])
    print("\n== LoCoMo cat2 时间题原文抽样（≤10）==")
    for s in cat2:
        print(f"  {s['qid']}")
        print(f"    gold: {s['gold']}")
        print(f"    pred: {s['pred']}")
        print(f"    F1: {s['score']}")

    out = {"lme": lme_attr, "loco_cat2_samples": cat2}
    if args.out:
        Path(args.out).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
