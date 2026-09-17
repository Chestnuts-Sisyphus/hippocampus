"""A/B 对照：同一批题、不同检索配置（嵌入模型／查询指令／注入条数）各跑一次，一次给出对比表。

为什么要单独一个脚本：`hippocampus bench` 固定用**配置里的**嵌入档，一次运行只出一个数；
比较"换模型／加不加查询指令"要么改配置跑多轮（容易串味），要么手工比对多份 JSON。
本脚本把"变体"显式化成参数：**每个变体一个独立数据根**（导入天然隔离，向量空间不互串），
同一批题（同 stride 抽样）保证可比。

口径与 `docs/benchmark.md` 一致：离线档（不发任何出站请求）、检索/词面口径，
报的是"金标准证据原文是否进注入上下文"（evidence_in_context）与"参考答案是否进上下文"，
**不是官方分**（官方要模型作答 + LLM 判分）。

用法（离线档；首次用某模型会联网下载一次 ONNX 到 ~/.hippocampus/models/）：

    HIPPOCAMPUS_OFFLINE=1 .venv/Scripts/python.exe scripts/bench_ab.py \
        --data D:/tmp/hc-bench/locomo10.json --limit 80 \
        --home-root D:/tmp/hc-bench/ab \
        --variants "onnx:Xenova/bge-small-en-v1.5|instr=off,onnx:Xenova/bge-small-en-v1.5|instr=on" \
        --json D:/tmp/hc-bench/ab_instr.json

变体语法：`<模型名>|instr=auto|on|off`（模型名与 `HIPPOCAMPUS_EMBEDDING_MODEL` 同语法）。
`instr=off/on` 会在该变体内强行关掉／打开"查询侧检索指令"（monkeypatch，不改生产默认）。
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

from hippocampus.core import MemoryCore, Scope  # noqa: E402
from hippocampus.eval import public_bench as pb  # noqa: E402
from hippocampus.memory import database as db  # noqa: E402
from hippocampus.memory import retrieval as rt  # noqa: E402

EN_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def parse_variant(spec: str) -> dict:
    body, _, opts = spec.partition("|")
    out = {"model": body.strip(), "instr": "auto"}
    for part in opts.split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def slug(variant: dict, *, home_scope: str = "variant") -> str:
    base = variant["model"].replace(":", "-").replace("/", "-") or "default"
    if home_scope == "model":
        return base
    return f"{base}-instr{variant['instr']}"


def apply_variant(variant: dict) -> None:
    """把变体写进进程环境（模型名走官方配置入口；指令开关走 monkeypatch）。"""
    from hippocampus.memory import embedding_models as em

    os.environ["HIPPOCAMPUS_EMBEDDING_MODEL"] = variant["model"]
    instr = variant["instr"]
    em._EXP_QUERY_INSTRUCTION_OVERRIDE = None  # noqa: SLF001
    if instr == "on":
        em._EXP_QUERY_INSTRUCTION_OVERRIDE = EN_QUERY_INSTRUCTION  # noqa: SLF001
    elif instr == "off":
        em._EXP_QUERY_INSTRUCTION_OVERRIDE = ""  # noqa: SLF001
    rt.invalidate_embedding_cache()


def _already_ingested(core: MemoryCore, scope: Scope) -> bool:
    """该账户库里已有记忆 → 视为导入过（`--reuse-import` 复用上一次导入）。"""
    try:
        session = core._session(scope)  # noqa: SLF001
        with session.lock:
            row = session.conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
            return bool(row and row["c"])
    except Exception:
        return False


def run_variant(
    variant: dict,
    items: list[pb.Item],
    home: str | Path,
    *,
    date_prefix: bool = True,
    reuse_import: bool = False,
    home_scope: str = "variant",
) -> dict:
    """跑一个变体：按组分池导入一次，然后逐题注入打分。"""
    apply_variant(variant)
    groups: dict[str, list[pb.Item]] = {}
    for item in items:
        groups.setdefault(item.group or item.qid, []).append(item)

    core = MemoryCore(home=home)
    rows: list[dict] = []
    t_ingest = 0.0
    reused = 0
    try:
        for gi, (_group, group_items) in enumerate(groups.items(), 1):
            scope = Scope(account=f"ab-{gi}", session="bench", source="user")
            if reuse_import and _already_ingested(core, scope):
                reused += 1
            else:
                t0 = time.perf_counter()
                core.ingest_history(
                    scope,
                    [pb._turn_dict(t, date_prefix) for t in group_items[0].turns],  # noqa: SLF001
                )
                t_ingest += time.perf_counter() - t0
            session = core._session(scope)  # noqa: SLF001
            with session.lock:
                db.set_active_params(session.conn, {"shadow_log_enabled": False}, reason="A/B 对照口径")
            for item in group_items:
                tq = time.perf_counter()
                injection = core.inject_finalize(scope, item.question)
                ms = (time.perf_counter() - tq) * 1000.0
                context = injection.text or ""
                norm = pb.normalize_text(context)
                rows.append(
                    {
                        "qid": item.qid,
                        "group": item.group,
                        "category": item.category,
                        "evidence": any(
                            pb.normalize_text(e)[:80] in norm for e in item.evidence_texts if pb.normalize_text(e)
                        ),
                        "answer": any(v and v in norm for a in item.answers for v in pb.answer_variants(a)),
                        "tokens": len(context) // 2 + 40 if context else 0,
                        "n_injected": len(injection.items),
                        "latency_ms": round(ms, 2),
                    }
                )
        print(
            f"  [{slug(variant, home_scope=home_scope)}] {len(groups)} 组 / {len(rows)} 题完成"
            f"（导入 {t_ingest:.1f}s，复用 {reused} 组）"
        )
    finally:
        core.close()

    def pct(vals: list[bool]) -> float:
        return round(100.0 * sum(1 for v in vals if v) / max(1, len(vals)), 2)

    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"] or "unknown", []).append(r)
    return {
        "variant": variant,
        "slug": slug(variant, home_scope=home_scope),
        "n": len(rows),
        "evidence_in_context": pct([r["evidence"] for r in rows]),
        "answer_in_context": pct([r["answer"] for r in rows]),
        "tokens_mean": round(sum(r["tokens"] for r in rows) / max(1, len(rows)), 1),
        "by_category": {
            k: {"n": len(v), "evidence_in_context": pct([r["evidence"] for r in v])}
            for k, v in sorted(by_cat.items())
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开基准 A/B 对照（离线档）")
    parser.add_argument("--data", required=True, help="LoCoMo JSON 路径")
    parser.add_argument("--home-root", required=True, help="各变体数据根的父目录")
    parser.add_argument("--variants", required=True, help="逗号分隔：<模型>|instr=auto|on|off")
    parser.add_argument("--limit", type=int, default=80, help="题量上限（0=全部）")
    parser.add_argument("--sample", choices=["stride", "head"], default="stride",
                        help="抽样方式：stride=按间隔跨对话抽（默认，覆盖全部对话）；head=取前 N 题")
    parser.add_argument("--convs", type=int, default=0, help="只用前 N 段对话（0=全部；省导入时间）")
    parser.add_argument("--home-scope", choices=["variant", "model"], default="variant",
                        help="数据根粒度：variant=每个变体一个（默认）；model=同模型共用"
                             "（只换查询指令时配合 --reuse-import 免重复导入）")
    parser.add_argument("--reuse-import", action="store_true", help="库里已有数据就跳过导入")
    parser.add_argument("--json", help="结果写入路径")
    args = parser.parse_args(argv)

    all_items = pb.load_locomo(args.data)
    if args.convs:
        keep = []
        seen = []
        for it in all_items:
            if it.group not in seen:
                if len(seen) >= args.convs:
                    continue
                seen.append(it.group)
            keep.append(it)
        all_items = keep
        print(f"限定前 {args.convs} 段对话（{len(seen)} 组）")
    if args.limit:
        if args.sample == "stride":
            stride = max(1, len(all_items) // args.limit)
            items = all_items[::stride][: args.limit]
        else:
            items = all_items[: args.limit]
    else:
        items = all_items
    print(f"题量 {len(items)}（抽样 {args.sample}／源 {len(all_items)}）")

    variants = [parse_variant(s) for s in args.variants.split(",") if s.strip()]
    reports = []
    for variant in variants:
        home = Path(args.home_root) / slug(variant, home_scope=args.home_scope)
        home.mkdir(parents=True, exist_ok=True)
        reports.append(
            run_variant(
                variant,
                items,
                home,
                reuse_import=args.reuse_import,
                home_scope=args.home_scope,
            )
        )

    print("\nA/B 对照（离线口径：金标准证据原文是否进注入上下文；非官方分）")
    print(f"{'变体':<46}{'题量':>6}{'证据在文内':>12}{'答在文内':>10}{'tokens':>9}")
    for rep in reports:
        print(
            f"{rep['slug']:<46}{rep['n']:>6}{rep['evidence_in_context']:>12}"
            f"{rep['answer_in_context']:>10}{rep['tokens_mean']:>9}"
        )
    base = reports[0]
    print("\n相对第一个变体的差值（证据命中，百分点）")
    for rep in reports[1:]:
        delta = round(rep["evidence_in_context"] - base["evidence_in_context"], 2)
        print(f"  {rep['slug']:<44}{delta:+.2f} pp")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "protocol": {
                        "offline": True,
                        "sample": args.sample,
                        "limit": args.limit,
                        "n_items": len(items),
                        "metric": "金标准证据原文进注入上下文（evidence_in_context）",
                        "not_official": "非官方分：无模型作答、无 LLM 判分",
                    },
                    "reports": reports,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
