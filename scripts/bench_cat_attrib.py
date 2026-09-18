"""分类低分归因（七轮 T8）：**只读既有官方判分 JSON**，零新花费、零出站。

为什么要有这个脚本（而不是"再跑一轮看看"）：本轮评测批次冻结（栗子 09-19 拍板），
所以归因必须建立在**已付过费的那批数据**上。方法沿用六轮 cat5 归因：
逐题按 `qid` 配对两次运行（基线臂／邻居扩展臂），数清"1→0／0→1"的错位，
再对目标分类做**机理拆分**（判分为 0 的题到底输在哪一环），最后给 ≤8 条 pred/gold 样例。

跑法（只读，不改判据、不重跑模型）：
    python scripts/bench_cat_attrib.py --category 3
    python scripts/bench_cat_attrib.py --category 3 --json D:/tmp/hc7/cat3.json

诚实边界：本脚本只做**归因**，不做提分；任何"顺着结论调阈值"的动作都不属于它。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

BENCH_DIR = Path("D:/tmp/hc-bench")
DEFAULT_RUNS = (BENCH_DIR / "loco_official_full.json", BENCH_DIR / "loco_nb_official.json")

# force-utf8 shim：Windows 控制台默认代码页打不出中文会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_rows(path: Path) -> dict[str, dict]:
    """一次运行的官方判分行表（qid → 行）。缺 official.rows 直接报错，不静默降级。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    official = payload.get("official") or {}
    rows = official.get("rows")
    if not rows:
        raise SystemExit(f"{path} 里没有 official.rows（这次运行没开官方判分臂？）")
    return {r["qid"]: r for r in rows if r.get("qid")}


def _load_dataset(path: Path) -> dict[str, dict]:
    """qid → 题目元信息（问题原文／金标／证据条数／分类）。qid 形如 `conv-26-q0`。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for sample in data:
        sid = sample.get("sample_id") or ""
        for j, qa in enumerate(sample.get("qa") or []):
            out[f"{sid}-q{j}"] = {
                "question": qa.get("question") or "",
                "gold": qa.get("answer"),
                "category": qa.get("category"),
                "evidence": len(qa.get("evidence") or []),
            }
    return out


def _passes(row: dict | None, threshold: float = 0.5) -> bool | None:
    """把官方分折成"过/不过"（F1 ≥ 0.5 记过）——与六轮 cat5 归因同一口径。"""
    if row is None or row.get("skipped") or row.get("error"):
        return None
    score = row.get("score")
    return None if score is None else float(score) >= threshold


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分类低分归因（只读既有官方判分 JSON）")
    ap.add_argument("--category", type=int, default=3, help="目标分类（LoCoMo cat1–cat5）")
    ap.add_argument("--runs", default=",".join(str(p) for p in DEFAULT_RUNS), help="两次运行的 JSON（逗号分隔）")
    ap.add_argument("--dataset", default=str(BENCH_DIR / "locomo10.json"), help="题目元信息来源（只读）")
    ap.add_argument("--max-samples", type=int, default=8)
    ap.add_argument("--json", help="把结果写到该路径")
    args = ap.parse_args(argv)

    paths = [Path(p.strip()) for p in args.runs.split(",") if p.strip()]
    if len(paths) < 2:
        raise SystemExit("需要两次运行才能做配对归因（--runs 传两个 JSON）")
    for p in (*paths, Path(args.dataset)):
        if not p.exists():
            raise SystemExit(f"找不到输入：{p}（评测批次冻结，本脚本不重跑，只读既有产物）")

    runs = [_load_rows(p) for p in paths]
    meta = _load_dataset(Path(args.dataset))
    names = [p.name for p in paths]
    cat = args.category

    common = [qid for qid in runs[0] if qid in runs[1] and meta.get(qid, {}).get("category") == cat]
    all_cat = [qid for qid in runs[0] if meta.get(qid, {}).get("category") == cat]
    print(f"目标分类 cat{cat}：{len(all_cat)} 题（两臂都有判分的 {len(common)} 题）")
    for name, rows in zip(names, runs, strict=True):
        scored = [float(rows[q]["score"]) for q in all_cat if rows[q].get("score") is not None]
        print(f"  {name}: 平均官方分 {statistics.mean(scored) * 100:.2f}%（n={len(scored)}）")

    base, other = runs[0], runs[1]
    flips_up = [q for q in common if _passes(base[q]) is False and _passes(other[q]) is True]
    flips_down = [q for q in common if _passes(base[q]) is True and _passes(other[q]) is False]
    both_zero = [q for q in common if _passes(base[q]) is False and _passes(other[q]) is False]
    print(f"\n配对（{names[0]} → {names[1]}）：0→1 {len(flips_up)} 题、1→0 {len(flips_down)} 题、"
          f"两臂都不过 {len(both_zero)} 题")

    # ---- 机理拆分：判分为 0 的题输在哪一环 ----
    zero_rows = [q for q in common if _passes(base[q]) is False]
    empty_pred = [q for q in zero_rows if not str(base[q].get("prediction") or "").strip()]
    gold_multi = [q for q in zero_rows if isinstance(meta[q]["gold"], list) or "," in str(meta[q]["gold"])]
    short_pred = [
        q for q in zero_rows
        if str(base[q].get("prediction") or "").strip() and len(str(base[q]["prediction"]).split()) <= 2
    ]
    evidence_counts = Counter(meta[q]["evidence"] for q in zero_rows)
    long_answers = [q for q in zero_rows if len(str(meta[q]["gold"]).split()) > 8]
    print("\n机理拆分（以基线臂不过的题为样本）：")
    print(f"  作答为空（模型没给出内容）        : {len(empty_pred)}/{len(zero_rows)}")
    print(f"  金标是多项/含逗号（需并列全对）    : {len(gold_multi)}/{len(zero_rows)}")
    print(f"  金标是长句（词面 F1 天然吃亏）     : {len(long_answers)}/{len(zero_rows)}")
    print(f"  作答极短（≤2 词，缺并列项的概率高） : {len(short_pred)}/{len(zero_rows)}")
    print(f"  证据条数分布（前 5 档）            : {evidence_counts.most_common(5)}")

    print(f"\n样例（≤{args.max_samples} 条，取两臂都不过的题）：")
    samples = []
    for q in both_zero[: args.max_samples]:
        item = {
            "qid": q,
            "question": meta[q]["question"][:160],
            "gold": str(meta[q]["gold"])[:160],
            "evidence_n": meta[q]["evidence"],
            "pred_base": str(base[q].get("prediction"))[:160],
            "score_base": round(float(base[q].get("score") or 0.0), 4),
            "pred_other": str(other[q].get("prediction"))[:160],
            "score_other": round(float(other[q].get("score") or 0.0), 4),
        }
        samples.append(item)
        print(f"  {q} 证据{item['evidence_n']}条  分={item['score_base']}/{item['score_other']}")
        print(f"    Q: {item['question']}")
        print(f"    金标: {item['gold']}")
        print(f"    基线臂作答: {item['pred_base']}")

    summary = {
        "category": cat,
        "n_category": len(all_cat),
        "n_paired": len(common),
        "runs": names,
        "mean_score_by_run": {
            name: round(statistics.mean([float(r[q]["score"]) for q in all_cat if r[q].get("score") is not None]) * 100, 2)
            for name, r in zip(names, runs, strict=True)
        },
        "flips_up": len(flips_up),
        "flips_down": len(flips_down),
        "both_fail": len(both_zero),
        "mechanism": {
            "failed_total": len(zero_rows),
            "empty_prediction": len(empty_pred),
            "gold_is_multivalued": len(gold_multi),
            "gold_is_long_form": len(long_answers),
            "prediction_very_short": len(short_pred),
            "evidence_count_hist": {str(k): v for k, v in sorted(evidence_counts.items())},
        },
        "samples": samples,
        "note": "只读既有官方判分 JSON；零新花费、不改判据、不调阈值",
    }
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
