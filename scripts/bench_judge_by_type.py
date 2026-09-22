"""GB4 零花费派生：官方判分批按 question_type 分组出「准确率 + Wilson 95% CI + 样本数」。

为什么需要：头条分 74.2%（LongMemEval-oracle 全量 n=500，官方判分臂，2026-09-18 批）
在报告 JSON 里只有 `official.by_category`（仅 n 与 accuracy，无区间），
分组结论此前无法独立复跑，也无法回答"某个题型到底几个题、区间多宽"。

本脚本是**切片器，不是评测器**（与 `bench_judge_subset.py` 同一性质）：

- **零花费**：不出站、不调模型、不读任何凭据环境变量；本文件**不 import hippocampus**，
  只用标准库做纯统计派生（不新增第三方依赖）；
- **判分口径与分数一律未动**：只把既有批 `official.rows` 的 `label` 按 `qid` 关联到
  顶层 `rows[].category`（= LongMemEval 的 question_type）后分组重算；
- **入仓产物只含聚合值**：逐题明细（question / gold / prediction / 注入上下文）在隐私面，
  绝不写进摘要；需要逐题明细时用 `--out-rows` 显式写到仓外路径。

跑法（只读，零出站，零花费；`--report` 指向仓外付费产物，不入仓）：

    python scripts/bench_judge_by_type.py \
        --report <官方判分臂报告 JSON> \
        --out results/lme_official_500b_by_type.json

输出：① 终端一张六题型表（含 knowledge-update 行）；② `--out` 指向的聚合摘要 JSON
（带输入文件 sha256、数据集 sha256 与报告自带口径字段，供外部按哈希核对同源）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

# force-utf8 shim：Windows 控制台默认代码页无法编码中文，会让打印把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# 隐私面字段黑名单：这些键一旦出现在**摘要**顶层，说明有人把逐题明细写进了入仓产物。
_PRIVATE_KEYS = ("question", "gold", "prediction", "judge_response", "context", "injected_kinds")


def _truthy(value: object) -> bool:
    """label 在 JSON 里可能是 bool、字符串，也可能未判（None／空）——与审计脚本同一口径。"""
    return str(value).strip().lower() in ("true", "1", "1.0", "yes")


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间（与 `bench_judge_subset.py`、报告 JSON 的 ci95 同一实现）。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _bucket(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {
        "n": n,
        "n_correct": k,
        "accuracy": round(100.0 * k / n, 2) if n else None,
        "ci95_wilson": [round(100.0 * lo, 2), round(100.0 * hi, 2)] if n else None,
    }


def build_type_index(report: dict) -> tuple[dict[str, str], int]:
    """从顶层 `rows` 取 qid → question_type 映射（LongMemEval 里 category 即题型）。"""
    index: dict[str, str] = {}
    missing = 0
    for row in report.get("rows") or []:
        qid = row.get("qid")
        if not qid:
            continue
        qtype = row.get("question_type") or row.get("category")
        if qtype:
            index[str(qid)] = str(qtype)
        else:
            missing += 1
    return index, missing


def derive(report: dict, source_sha256: str, source_name: str) -> dict:
    official = report.get("official") or {}
    type_index, n_rows_without_type = build_type_index(report)

    groups: dict[str, list[int]] = {}
    unjoined: list[str] = []
    n_unjudged = 0
    n_skipped = 0
    for row in official.get("rows") or []:
        qid = str(row.get("qid") or "")
        if not qid:
            continue
        if row.get("skipped"):
            n_skipped += 1
            continue
        qtype = type_index.get(qid)
        if qtype is None:
            unjoined.append(qid)
            continue
        if row.get("label") is None:
            n_unjudged += 1
            continue
        bucket = groups.setdefault(qtype, [0, 0])
        bucket[1] += 1
        if _truthy(row.get("label")):
            bucket[0] += 1

    by_type = {qtype: _bucket(k, n) for qtype, (k, n) in sorted(groups.items())}
    total_n = sum(v["n"] for v in by_type.values())
    total_k = sum(v["n_correct"] for v in by_type.values())

    # 与报告自带 by_category 逐条对账（不一致就是 join 或口径出了问题，暴露出来而不是掩盖）
    report_by_cat = official.get("by_category") or {}
    cross_check: list[dict] = []
    for qtype, value in by_type.items():
        ref = report_by_cat.get(qtype)
        ref_acc = round(100.0 * float(ref["accuracy"]), 2) if ref and ref.get("accuracy") is not None else None
        cross_check.append(
            {
                "question_type": qtype,
                "derived_n": value["n"],
                "report_n": ref.get("n") if ref else None,
                "derived_accuracy": value["accuracy"],
                "report_accuracy": ref_acc,
                "match": bool(ref) and value["n"] == ref.get("n") and value["accuracy"] == ref_acc,
            }
        )

    return {
        "derived_from": "官方判分臂报告的 official.rows × 顶层 rows[].category（按 qid 关联），"
                        "零花费纯派生：判分口径与分数未动",
        "source": {
            "file_name": source_name,
            "sha256": source_sha256,
            "dataset_sha256": report.get("dataset_sha256"),
            "bench": report.get("bench"),
            "generated_at": report.get("generated_at"),
            "n_items": official.get("n_items"),
            "n_done": official.get("n_done"),
            "n_failed": official.get("n_failed"),
            "n_skipped": official.get("n_skipped"),
        },
        "protocol": {
            "model": official.get("model"),
            "judge": official.get("judge", "deepseek-judge"),
            "input_spec": official.get("input_spec"),
            "metric": official.get("metric"),
            "report_accuracy": official.get("accuracy"),
            "report_ci95": official.get("ci95"),
        },
        "overall": _bucket(total_k, total_n),
        "by_question_type": by_type,
        "join": {
            "n_types": len(by_type),
            "n_qid_with_question_type": len(type_index),
            "n_rows_without_question_type": n_rows_without_type,
            "n_qid_unjoined": len(unjoined),
            "n_unjudged": n_unjudged,
            "n_skipped": n_skipped,
        },
        "cross_check_vs_report_by_category": cross_check,
        "privacy": {
            "per_question_rows_included": False,
            "note": "本摘要只含聚合值（n／对数／准确率／Wilson 区间）与哈希；"
                    "逐题 question／gold／prediction／注入上下文不写入，需要时另写仓外明细件。",
        },
    }


def render_table(summary: dict) -> str:
    lines = [
        f"{'题型':<30}{'n':>5}{'对':>5}{'准确率':>10}   Wilson 95% CI",
        "-" * 74,
    ]
    for qtype, v in summary["by_question_type"].items():
        lo, hi = v["ci95_wilson"] or (None, None)
        lines.append(
            f"{qtype:<30}{v['n']:>5}{v['n_correct']:>5}{v['accuracy']:>9.2f}%   {lo:.2f}% – {hi:.2f}%"
        )
    o = summary["overall"]
    lines.append("-" * 74)
    lines.append(
        f"{'合计（全部题型）':<30}{o['n']:>5}{o['n_correct']:>5}{o['accuracy']:>9.2f}%   "
        f"{o['ci95_wilson'][0]:.2f}% – {o['ci95_wilson'][1]:.2f}%"
    )
    return "\n".join(lines)


def write_rows(path: Path, report: dict) -> int:
    """把逐题明细写到**仓外**路径（显式要求才写；这是隐私面，不入仓）。"""
    type_index, _ = build_type_index(report)
    rows = []
    for row in (report.get("official") or {}).get("rows") or []:
        qid = str(row.get("qid") or "")
        rows.append({**row, "question_type": type_index.get(qid)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="官方判分批按 question_type 分组（零花费纯派生）")
    ap.add_argument("--report", required=True, help="既有官方判分臂报告 JSON（仓外付费产物）")
    ap.add_argument("--out", help="聚合摘要写入路径（建议入仓；只含聚合值与哈希）")
    ap.add_argument("--out-rows", help="逐题明细写入路径（隐私面，必须指向仓外，默认不写）")
    args = ap.parse_args(argv)

    src = Path(args.report)
    raw = src.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    summary = derive(report, hashlib.sha256(raw).hexdigest(), src.name)

    # 硬闸：入仓摘要不得夹带逐题隐私字段
    leaked = [k for k in _PRIVATE_KEYS if k in summary]
    if leaked:
        sys.exit(f"摘要里出现隐私面字段 {leaked}——派生件不得带逐题明文")

    print(render_table(summary))
    print()
    join = summary["join"]
    print(f"关联：题型类别 {join['n_types']} 个（带题型的 qid {join['n_qid_with_question_type']} 个）；"
          f"未关联 qid {join['n_qid_unjoined']}；未判 {join['n_unjudged']}；跳过 {join['n_skipped']}")
    mismatched = [r for r in summary["cross_check_vs_report_by_category"] if not r["match"]]
    print(f"对账：与报告自带 by_category 不一致 {len(mismatched)} 项"
          f"{'（' + str(mismatched) + '）' if mismatched else '（逐题型 n 与准确率逐位一致）'}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n聚合摘要已写入 {out}")
    if args.out_rows:
        n = write_rows(Path(args.out_rows), report)
        print(f"逐题明细已写入 {args.out_rows}（{n} 行；隐私面，勿入仓）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
