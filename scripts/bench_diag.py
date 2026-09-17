"""失败归因诊断（LoCoMo）：证据没进上下文时，它到底差在哪一步？

回答三个问题（同一批题、同一份导入；离线档，不发任何出站请求）：
① **命中率**：金标准证据原文是否进了注入上下文；
② **邻居可达性**：没命中时，"证据所在轮的上一轮／下一轮"是否进了上下文——
   这是"±1 轮扩展"能捞回来的**上界**（若邻居也不在，扩展就白扩）；
③ **本会话可达性**：没命中时，上下文里是否至少有**同一会话**的其它轮——
   说明"会话级召回"到了而"轮级定位"没到（那是排序问题，不是召回问题）。

用法（沿用 A/B 的数据根，免重复导入）：

    HIPPOCAMPUS_OFFLINE=1 .venv/Scripts/python.exe scripts/bench_diag.py \
        --data D:/tmp/hc-bench/locomo10.json --home D:/tmp/hc-bench/ab/onnx-Xenova-bge-small-en-v1.5 \
        --convs 3 --limit 80 --json D:/tmp/hc-bench/diag_neighbors.json
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


def _norm_ctx(text: str) -> str:
    return pb.normalize_text(text or "")


def classify_miss(turn_norms: list[str], ctx: str, evidence_turn_idxs: list[int]) -> tuple[bool, bool]:
    """未命中题的邻居/同会话判据（E3/G8：**整句精确匹配**，不再用短前缀）。

    旧版用 40/60 字符前缀判"轮在上下文"——前缀可能撞上别的轮或别的注入内容，
    把"邻居已在上下文"高估（这也是 ±1 轮扩展上界 +22.5pp 的来源之一）。
    现在改为 normalize 后**整句**在上下文里才算"在"：
    - `neighbor_in_ctx`：证据轮的上一轮/下一轮整句在上下文；
    - `same_session_in_ctx`：同会话**另有其它轮**（排除证据轮自身）整句在上下文。

    返回 (neighbor_in_ctx, same_session_in_ctx)。注意：整句匹配要求注入内容=原文整句
    （基准导入口径按原文整句落库，满足），若记忆被截断会**低估**——宁可低估不高估。
    """
    ctx_set = {t for t in turn_norms if t and t in ctx}
    nb_hit = False
    for i in evidence_turn_idxs:
        for j in (i - 1, i + 1):
            if 0 <= j < len(turn_norms) and turn_norms[j] and turn_norms[j] in ctx:
                nb_hit = True
    ev_set = {turn_norms[i] for i in evidence_turn_idxs if i < len(turn_norms)}
    other = ctx_set - ev_set
    return nb_hit, bool(other)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoCoMo 失败归因诊断（离线档）")
    parser.add_argument("--data", required=True)
    parser.add_argument("--home", required=True, help="已有导入的数据根（缺则当场导入）")
    parser.add_argument("--convs", type=int, default=0, help="只用前 N 段对话（0=全部）")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--sample", choices=["stride", "head"], default="stride")
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    items = pb.load_locomo(args.data)
    if args.convs:
        keep, seen = [], []
        for it in items:
            if it.group not in seen:
                if len(seen) >= args.convs:
                    continue
                seen.append(it.group)
            keep.append(it)
        items = keep
    if args.limit:
        if args.sample == "stride":
            stride = max(1, len(items) // args.limit)
            items = items[::stride][: args.limit]
        else:
            items = items[: args.limit]

    groups: dict[str, list[pb.Item]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it)

    core = MemoryCore(home=args.home)
    total = hit = 0
    miss_prev_next = miss_same_session = miss_nothing = 0
    rows: list[dict] = []
    try:
        for gi, (_group, group_items) in enumerate(groups.items(), 1):
            scope = Scope(account=f"ab-{gi}", session="bench", source="user")
            turns = group_items[0].turns
            turn_norms = [pb.normalize_text(t.text) for t in turns]
            for item in group_items:
                injection = core.inject_finalize(scope, item.question)
                ctx = _norm_ctx(injection.text)
                total += 1
                ev = [pb.normalize_text(e)[:80] for e in item.evidence_texts if pb.normalize_text(e)]
                if any(e in ctx for e in ev):
                    hit += 1
                    rows.append({"qid": item.qid, "category": item.category, "status": "hit"})
                    continue
                # 证据轮在会话中的下标（按文本前缀匹配；精判据在 classify_miss 里）
                idxs = []
                for i, tn in enumerate(turn_norms):
                    if any(tn[:80].startswith(e[:60]) or e[:60] in tn[:80] for e in ev):
                        idxs.append(i)
                nb_hit, sess_hit = classify_miss(turn_norms, ctx, idxs)
                if nb_hit:
                    miss_prev_next += 1
                elif sess_hit:
                    miss_same_session += 1
                else:
                    miss_nothing += 1
                rows.append(
                    {
                        "qid": item.qid,
                        "category": item.category,
                        "status": "miss",
                        "evidence_turn_found": bool(idxs),
                        "neighbor_in_ctx": nb_hit,
                        "same_session_in_ctx": sess_hit,
                    }
                )
    finally:
        core.close()

    miss = total - hit
    out = {
        "n": total,
        "evidence_in_context": round(100.0 * hit / max(1, total), 2),
        "miss": miss,
        "miss_neighbor_in_ctx": miss_prev_next,
        "miss_same_session_in_ctx": miss_same_session,
        "miss_nothing": miss_nothing,
        "neighbor_reachable_share_of_misses": round(100.0 * miss_prev_next / max(1, miss), 2),
        "rows": rows,
    }
    print(f"题量 {total}　证据命中 {out['evidence_in_context']}%（未命中 {miss}）")
    print(f"  未命中里「证据轮的上一轮/下一轮在上下文」：{miss_prev_next}（占未命中 "
          f"{out['neighbor_reachable_share_of_misses']}%）← ±1 轮扩展的上界")
    print(f"  未命中里「至少同会话有其它轮在上下文」：{miss_same_session}")
    print(f"  未命中里「整个会话都没进上下文」：{miss_nothing}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
