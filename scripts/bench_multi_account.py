#!/usr/bin/env python
"""T4 多账户/长跑常态压测（A15）：200 账户合成数据，报告峰值常驻内存 + 证据命中。

为什么：内存上限此前只在评测时撞出（LME 一题一账户 200 组 ≈4GB）。本脚本把
"多账户长跑"变成**日常可跑**的合成压测：200 个账户，每账户 3 条记忆 + 1 次查询
（答案文本在库内），离线档零凭据零出站。测两件事：
- **证据命中**：每账户自己的记忆被自己查询检回的比例（LRU 逐出不该丢这个）；
- **峰值常驻内存**：进程工作集峰值（LRU 上限 `HIPPOCAMPUS_SESSION_CACHE_MAX`
  生效时，200 账户不应当打爆内存）。

用法（离线，零下载；CI 可跑）：
    python scripts/bench_multi_account.py --accounts 200 --json D:/tmp/multi_account.json
看门限（--sentinel 时低于则退出码 1）：证据命中 ≥98%、峰值 < 2000 MB。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import psutil  # noqa: E402  # 内存采样：已进 pyproject dev extras（C6 六轮）
except ImportError:  # 非 dev 环境缺装：内存读数恒 0（峰值行如实标 0），进程照跑
    psutil = None  # type: ignore[assignment]

from hippocampus.core import MemoryCore, Scope  # noqa: E402


def _rss_bytes() -> int:
    if psutil is None:
        return 0
    return int(psutil.Process().memory_info().rss)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多账户常态压测（合成数据，离线）")
    ap.add_argument("--home", default="D:/tmp/hc-bench/multi-account", help="压测数据根（D 盘）")
    ap.add_argument("--accounts", type=int, default=200)
    ap.add_argument("--memos-per-account", type=int, default=3)
    ap.add_argument("--cache-max", type=int, default=16, help="会话缓存上限（对应 LRU）")
    ap.add_argument("--sentinel", action="store_true", help="启用看门限：证据命中<98% 或峰值≥2000MB 则退出码 1")
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    os.environ["HIPPOCAMPUS_SESSION_CACHE_MAX"] = str(max(1, min(int(args.cache_max), 512)))
    home = Path(args.home).expanduser().resolve()
    if home.exists():
        import shutil

        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    core = MemoryCore(home=home)
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            rss = _rss_bytes()
            if rss and rss > peak:
                peak = rss
            time.sleep(0.1)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    try:
        t0 = time.perf_counter()
        hits = total = 0
        rows: list[dict] = []
        for i in range(int(args.accounts)):
            acc = f"ma-{i}"
            scope = Scope(account=acc, session="s1", source="user")
            text = f"账户 {i} 的独有存档：仅本账户能回答自己的查询标记 i{i}"
            for j in range(int(args.memos_per_account)):
                core.write(scope, f"{text} 备注{j}", kind="fact")
            result = core.search(scope, f"账户 {i} 的独有存档", limit=8)
            hit = any(text in item.content for item in result.items)
            hits += int(hit)
            total += 1
            rows.append({"account": acc, "evidence_hit": bool(hit), "n_injected": len(result.items)})
        elapsed = time.perf_counter() - t0
    finally:
        stop.set()
        th.join(timeout=3)
        core.close()

    hit_rate = round(100.0 * hits / max(1, total), 2)
    peak_mb = round(peak / 1024 / 1024, 1)
    healthy = hit_rate >= 98.0 and peak_mb < 2000.0
    out = {
        "accounts": int(args.accounts),
        "memos_per_account": int(args.memos_per_account),
        "cache_max": int(args.cache_max),
        "evidence_hit_rate_pct": hit_rate,
        "peak_rss_mb": peak_mb,
        "elapsed_s": round(elapsed, 1),
        "healthy": healthy,
        "rows": rows,
    }
    print(f"账户 {out['accounts']}（每账户 {out['memos_per_account']} 条记忆 + 1 次查询）")
    print(f"证据命中 {hit_rate}%（阈值 98）  峰值常驻 {peak_mb} MB（看门限 2000 MB）  "
          f"{elapsed:.1f}s  → {'绿' if healthy else '红'}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json}")
    if args.sentinel and not healthy:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
