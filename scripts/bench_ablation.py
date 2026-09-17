"""消融对照：同一份导入下，不同注入条数上限 / 不同通道配置的效果（一次导入多次查询）。

用法（离线档）：
    HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" \
    python scripts/bench_ablation.py --data D:/tmp/hc-bench/locomo10.json --home D:/tmp/hc-bench/run-abl

为什么单独一个脚本：`hippocampus bench` 每次运行都要重新导入（大语料下导入占大头），
而"注入条数 8／20／50"这类消融只需要换一个参数——一次导入、多次查询即可，省一大截时间。
口径与 `docs/benchmark.md` 一致（离线、检索/词面口径，非官方分）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hippocampus.core import MemoryCore, Scope  # noqa: E402
from hippocampus.eval import public_bench as pb  # noqa: E402
from hippocampus.memory import database as db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开基准消融（一次导入、多次查询）")
    parser.add_argument("--data", required=True, help="LoCoMo JSON 路径")
    parser.add_argument("--home", required=True, help="基准数据根（与用户库隔离）")
    parser.add_argument("--limit", type=int, default=0, help="题量上限（0=全部）")
    parser.add_argument("--ks", default="8,20,50", help="要对比的注入条数上限，逗号分隔")
    parser.add_argument("--json", help="结果写入路径")
    args = parser.parse_args(argv)

    items = pb.load_locomo(args.data)
    if args.limit:
        items = items[: args.limit]
    groups: dict[str, list[pb.Item]] = {}
    for item in items:
        groups.setdefault(item.group, []).append(item)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    core = MemoryCore(home=args.home)
    reports: dict[str, dict] = {}
    try:
        for gi, (_group, group_items) in enumerate(groups.items(), 1):
            scope = Scope(account=f"abl-{gi}", session="bench", source="user")
            core.ingest_history(scope, [pb._turn_dict(t, True) for t in group_items[0].turns])  # noqa: SLF001
            session = core._session(scope)  # noqa: SLF001
            for k in ks:
                with session.lock:
                    db.set_active_params(session.conn, {"injection_max_items": int(k)}, reason=f"消融 k={k}")
                rows = []
                for item in group_items:
                    injection = core.inject_finalize(scope, item.question)
                    ctx = pb.normalize_text(injection.text or "")
                    rows.append(
                        {
                            "answer": any(v and v in ctx for a in item.answers for v in pb.answer_variants(a)),
                            "evidence": any(
                                pb.normalize_text(e)[:80] in ctx for e in item.evidence_texts if pb.normalize_text(e)
                            ),
                            "tokens": len(injection.text or "") // 2 + 40 if injection.text else 0,
                        }
                    )
                key = f"k={k}"
                agg = reports.setdefault(key, {"n": 0, "answer": 0, "evidence": 0, "tokens": 0})
                agg["n"] += len(rows)
                agg["answer"] += sum(1 for r in rows if r["answer"])
                agg["evidence"] += sum(1 for r in rows if r["evidence"])
                agg["tokens"] += sum(r["tokens"] for r in rows)
            print(f"  组 {gi}/{len(groups)} 完成（{group_items[0].group}）")
    finally:
        core.close()

    print("\n注入条数消融（离线口径：证据/答案是否进上下文）")
    print(f"{'配置':<8}{'题量':>6}{'答在文内':>10}{'证据在文内':>12}{'tokens':>9}")
    out = {}
    for key, agg in reports.items():
        n = max(1, agg["n"])
        row = {
            "n": agg["n"],
            "answer_in_context": round(100 * agg["answer"] / n, 2),
            "evidence_in_context": round(100 * agg["evidence"] / n, 2),
            "tokens_mean": round(agg["tokens"] / n, 1),
        }
        out[key] = row
        print(f"{key:<8}{row['n']:>6}{row['answer_in_context']:>10}{row['evidence_in_context']:>12}{row['tokens_mean']:>9}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
