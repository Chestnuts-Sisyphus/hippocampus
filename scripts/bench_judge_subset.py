"""X14（十轮）：从既有官方判分批产物派生 **n=400 判分产物 JSON**（零花费、只读、不调端点）。

付费评测批次冻结 → 不重新作答、不重新判分，只按 `qid` 升序取既有批次 `official.rows` 的
前 N 题，重算该子集的准确率与 Wilson CI，落一份可审计的派生产物。
判分口径与分数**一律未动**——本脚本是切片器，不是评测器。

跑法：
    python scripts/bench_judge_subset.py --report D:/tmp/hc-bench/lme_official_500.json \
        --n 400 --out D:/tmp/hc-bench/lme_official_400.json
"""

from __future__ import annotations

import argparse
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


def _truthy(value: object) -> bool:
    """label 在 JSON 里可能是 bool、字符串，也可能未判（None／空）——与审计脚本同一口径。"""
    return str(value).strip().lower() in ("true", "1", "1.0", "yes")


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def derive(report: dict, n: int) -> dict:
    official = report.get("official") or {}
    rows = [r for r in (official.get("rows") or []) if r.get("qid")]
    rows_sorted = sorted(rows, key=lambda r: str(r["qid"]))
    subset = rows_sorted[:n]

    judged = [r for r in subset if r.get("label") is not None]
    correct = sum(1 for r in judged if _truthy(r.get("label")))
    k, m = correct, len(judged)
    lo, hi = wilson_ci(k, m)

    return {
        "bench": report.get("bench"),
        "derived_from": "official.rows 按 qid 升序取前 N 题（零花费派生，判分口径未动）",
        "protocol": report.get("protocol"),
        "official": {
            "model": official.get("model"),
            "judge": official.get("judge", "deepseek-judge"),
            "n_total_rows": len(rows_sorted),
            "n_subset": len(subset),
            "n_judged": m,
            "n_correct": k,
            "accuracy": round(100.0 * k / m, 1) if m else None,
            "ci95_wilson": [round(100.0 * lo, 1), round(100.0 * hi, 1)] if m else None,
        },
        "rows": subset,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="既有官方臂报告 JSON")
    ap.add_argument("--n", type=int, default=400, help="取前 N 题（默认 400）")
    ap.add_argument("--out", required=True, help="派生产物写入路径")
    args = ap.parse_args(argv)

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    result = derive(report, args.n)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    o = result["official"]
    print(f"派生产物已写入 {out}")
    print(f"  n_subset={o['n_subset']}  n_judged={o['n_judged']}  accuracy={o['accuracy']}%  CI95={o['ci95_wilson']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
