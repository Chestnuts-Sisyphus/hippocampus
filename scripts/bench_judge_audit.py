"""八轮 V11：judge 一致性的**零花费**扩样审计（离线，只读既有批次产物，不调任何端点）。

背景（正本 §10.6 与 `docs/benchmark.md` §二·三）：官方分是 LLM judge 判的，
六轮只做过 **50 题**双判（deepseek-chat vs glm-4-flash，分歧 8.0%），**无人工裁决**，
所以"judge 模型差异会不会让数字失真"这个问题**结论未定**。付费评测批次冻结 → 本轮一分钱不花，
只做三件机器能做的事：

1. **判分实现层重推导**：把每批 `official.rows` 里记着的 `judge_response` 按官方逐字规则
   （`'yes' in lower` 为对）**重推一遍标签**，与落库的 `label` 逐条比对——
   这钉的是"我们自己把 judge 的话折成对/错时有没有折错"，n 等于批内判分次数；
2. **跨批复判可行性检查**：两批官方臂报告按 `qid` 求交集，再分两个口径数分歧——
   **prediction 完全一致**的子集（那才是纯 judge 抖动）与全体交集（混了作答漂移，只能当参考）。
   顺带把 label 的存储形态查清楚：**`label` 可能是 `None`（该批未判成），
   用 `bool(label)` 判真假会把 `None`/`"False"` 都当成真，必须显式解析**（本轮实测踩过）；
3. **人工抽判样本导出**：按确定性顺序（qid 升序）挑出 judge 判"否"的前 N 题，
   连 question/gold/prediction 一起导出，供人工裁决用（本脚本不自动裁决、不改任何分数）。

跑法（只读，零出站）：
    python scripts/bench_judge_audit.py --reports D:/tmp/hc-bench/lme_official_500b.json \\
        --cross D:/tmp/hc-bench/lme_official_500.json --sample 20 --out D:/tmp/hc8/judge_audit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _truthy(value: object) -> bool:
    """label 在 JSON 里可能是 bool、字符串，也可能**根本没判**（None／空）。"""
    return str(value).strip().lower() in ("true", "1", "1.0", "yes")


def _official_rows(report: dict) -> dict[str, dict]:
    rows = ((report.get("official") or {}).get("rows")) or []
    return {str(r["qid"]): r for r in rows if r.get("qid")}


def _meta_rows(report: dict) -> dict[str, dict]:
    rows = report.get("rows") or []
    return {str(r["qid"]): r for r in rows if r.get("qid")}


def audit(report_path: Path, cross_path: Path | None, sample_n: int) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = _official_rows(report)
    meta = _meta_rows(report)

    derived_mismatch = [
        qid
        for qid, r in rows.items()
        if _truthy(r.get("label")) != ("yes" in str(r.get("judge_response") or "").lower())
    ]
    judged = [qid for qid, r in rows.items() if r.get("label") is not None]
    unjudged = [qid for qid, r in rows.items() if r.get("label") is None]

    out: dict = {
        "report": str(report_path),
        "bench": report.get("bench"),
        "n_official_rows": len(rows),
        "n_label_null": len(unjudged),
        "n_judged": len(judged),
        "accuracy_in_report": (report.get("official") or {}).get("accuracy"),
        "derive_labels_mismatch": len(derived_mismatch),
        "derive_labels_mismatch_qids": derived_mismatch[:10],
    }

    if cross_path is not None and cross_path.exists():
        other = _official_rows(json.loads(cross_path.read_text(encoding="utf-8")))
        common = sorted(set(rows) & set(other))
        same_pred = [q for q in common if str(rows[q].get("prediction")) == str(other[q].get("prediction"))]
        diff_all = [q for q in common if _truthy(rows[q].get("label")) != _truthy(other[q].get("label"))]
        diff_same = [q for q in same_pred if _truthy(rows[q].get("label")) != _truthy(other[q].get("label"))]
        out["cross"] = {
            "report": str(cross_path),
            "n_common": len(common),
            "n_same_prediction": len(same_pred),
            "n_label_diff_all": len(diff_all),
            "n_label_diff_same_prediction": len(diff_same),
            "usable_as_pure_judge_repeat": bool(len(same_pred) >= 2) and len(unjudged) == 0 and len(other) and all(
                r.get("label") is not None for r in other.values()
            ),
            "note": (
                "两批要能当 judge 复判用，必须 ①都有真标签（None 不算）②prediction 一致；"
                "否则分歧里混着作答漂移，只能当参考"
            ),
        }

    sample = []
    for qid in sorted(rows):
        if len(sample) >= sample_n:
            break
        r = rows[qid]
        if r.get("label") is None or _truthy(r.get("label")):
            continue  # 只抽 judge 判"否"的题：judge 若误杀，这里是人工裁决最有信息量的地方
        m = meta.get(qid) or {}
        sample.append(
            {
                "qid": qid,
                "question": str(m.get("question") or "")[:220],
                "gold": str(m.get("gold") or "")[:220],
                "prediction": str(r.get("prediction") or "")[:320],
                "judge_response": str(r.get("judge_response") or "")[:60],
            }
        )
    out["manual_sample"] = sample
    out["manual_sample_n"] = len(sample)
    return out


# force-utf8 shim：Windows 控制台默认代码页（cp1252）无法编码 ✓ 等字符，会让"打印"把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="要审计的官方臂报告 JSON")
    ap.add_argument("--cross", help="第二份官方臂报告（做跨批复判可行性检查）")
    ap.add_argument("--sample", type=int, default=20, help="人工抽判样本量（默认 20）")
    ap.add_argument("--out", help="把结果写到该路径")
    args = ap.parse_args(argv)

    result = audit(Path(args.report), Path(args.cross) if args.cross else None, args.sample)
    print(json.dumps({k: v for k, v in result.items() if k != "manual_sample"}, ensure_ascii=False, indent=2))
    print(f"人工抽判样本：{result['manual_sample_n']} 题（judge 判否的题，qid 升序），明细在 --out 的 JSON 里")
    if result["n_label_null"]:
        print(f"  注意：该批有 {result['n_label_null']} 题 label 为 None（未判成）——不能拿它当第二判用")
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
