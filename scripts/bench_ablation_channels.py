#!/usr/bin/env python
"""T9 逐通道消融 + 候选集哨兵（A5/A6）：LoCoMo 四通道各自关掉的全量对照。

为什么需要：既有消融只有"记忆条 vs 经历层"粗口径；四个通道（语义/BM25/图/事件）
各自的贡献从未被隔离测量。本脚本每个变体一个独立数据根（同 `bench_ab` 的隔离），
同一批题（同 stride 抽样），只改 `retrieval._ABLATION_CHANNELS` 一个变量。

用法（离线档；神经档首次会联网下载 ONNX 一次，之后纯本地）：

    HIPPOCAMPUS_OFFLINE=1 HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-en-v1.5" \
      python scripts/bench_ablation_channels.py \
        --data D:/tmp/hc-bench/locomo10.json --convs 3 \
        --json D:/tmp/hc-bench/ablation_channels.json

输出：全开基线 vs 各关一个通道的 证据在文内/答在文内/tokens/p50；结论一句话由人读表。

哨兵（A5）：--sentinel-threshold 45.0（神经档证据命中下限）——任一"全开"档结果
低于阈值则退出码 1（CI 用）；容差说明写进输出。默认只报告不红（--sentinel 才红）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hippocampus.core import MemoryCore  # noqa: E402
from hippocampus.eval import public_bench as pb  # noqa: E402
from hippocampus.memory import retrieval as rt  # noqa: E402

CHANNELS = ("semantic", "bm25", "graph", "event")
SENTINEL_DEFAULT = 45.0  # 神经档证据命中下限（结果文档 §二：45.7%，留 0.7pp 容差）


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T9 逐通道消融（LoCoMo，离线）")
    ap.add_argument("--data", required=True)
    ap.add_argument("--home-root", default="D:/tmp/hc-bench/abl-ch", help="各变体数据根父目录")
    ap.add_argument("--convs", type=int, default=0, help="只用前 N 段对话（0=全部；3 段≈600 题）")
    ap.add_argument("--limit", type=int, default=0, help="题量上限（0=全部）")
    ap.add_argument("--json")
    ap.add_argument("--sentinel", action="store_true", help="启用哨兵：全开档证据命中 < 阈值则退出码 1")
    ap.add_argument("--sentinel-threshold", type=float, default=SENTINEL_DEFAULT)
    args = ap.parse_args(argv)

    all_items = pb.load_locomo(args.data)
    if args.convs:
        keep, seen = [], []
        for it in all_items:
            if it.group not in seen:
                if len(seen) >= args.convs:
                    continue
                seen.append(it.group)
            keep.append(it)
        all_items = keep
    if args.limit:
        stride = max(1, len(all_items) // args.limit)
        all_items = all_items[::stride][: args.limit]
    print(f"题量 {len(all_items)}（对话 {len({it.group for it in all_items})} 段，源 {len(all_items)}→抽样后）")

    variants: list[dict] = [{"name": "全开", "abl": None}] + [{"name": f"关{f}", "abl": f} for f in CHANNELS]
    reports = []
    root = Path(args.home_root)
    for i, v in enumerate(variants):
        rt._ABLATION_CHANNELS = set(v["abl"]) if v["abl"] else None  # noqa: SLF001 - 消融钩子
        home = root / (v["name"])
        groups: dict[str, list[pb.Item]] = {}
        for it in all_items:
            groups.setdefault(it.group or it.qid, []).append(it)
        core = MemoryCore(home=home)
        t0 = time.perf_counter()
        try:
            report = pb.run_items(
                core,
                all_items,
                account_prefix=f"abl{i}",
                split_pools=True,
                progress_every=len(groups),
            )
        finally:
            core.close()
        rt._ABLATION_CHANNELS = None  # noqa: SLF001
        o = report["overall"]
        reports.append(
            {
                "variant": v["name"],
                "n": o["n"],
                "evidence_in_context": o["evidence_in_context"],
                "answer_in_context": o["answer_in_context"],
                "tokens_mean": o["tokens_mean"],
                "latency_p50_ms": o["latency_p50_ms"],
                "elapsed_s": round(time.perf_counter() - t0, 1),
            }
        )
        print(
            f"  [{v['name']}] 证据在文内 {o['evidence_in_context']}%  答在文内 "
            f"{o['answer_in_context']}%  tokens {o['tokens_mean']}  p50 {o['latency_p50_ms']}ms"
            f"（{time.perf_counter() - t0:.1f}s）"
        )

    base = reports[0]
    print("\n逐通道消融（相对全开的证据命中差值，百分点）")
    for r in reports[1:]:
        print(f"  {r['variant']:<10}{r['evidence_in_context'] - base['evidence_in_context']:+.2f} pp")

    healthy = base["evidence_in_context"] >= args.sentinel_threshold
    sentinel_on = bool(args.sentinel)
    print(
        f"\n哨兵：全开证据命中 {base['evidence_in_context']}%（阈值 {args.sentinel_threshold}；"
        f"容差 0.7pp 来自基线与历史 45.7% 的差异）→ {'绿' if healthy else '红'}"
        + ("（启用，红则退出码 1）" if sentinel_on else "（仅报告，未启用）")
    )
    if args.json:
        payload = {
            "protocol": {
                "offline": True,
                "metric": "金标准证据原文进注入上下文（evidence_in_context）+ 答在文内",
                "ablation_hook": "retrieval._ABLATION_CHANNELS（默认 None＝生产零变化）",
                "convs": args.convs,
                "n_items": len(all_items),
            },
            "reports": reports,
            "sentinel": {
                "enabled": sentinel_on,
                "threshold": args.sentinel_threshold,
                "baseline_hit": base["evidence_in_context"],
                "healthy": healthy,
            },
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    if sentinel_on and not healthy:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
