"""真机变更句实测（七轮 T5 / HC-0901-01·0816-02 验收）：不钉桩，走真实写入通道。

和单元测试的分工（为什么要单独有这个脚本）：
- `tests/test_a29_p1_no_drop.py` 把"语义去重命中"**钉死**（monkeypatch），验的是命中
  之后的处置分支，不是真机链路本身；
- 前身缺陷是"现网 MiniLM 8/14 变更句被吞"——只有**真机端到端**（当前嵌入档 → 真实
  相似度 → 真实去重/冲突判定 → 真实落库状态）能复现那类问题。

覆盖设计里的三条去路（都不许静默丢，A29／A24）：
    supersede  机械确认值变更 → 旧条立即转 superseded
    admit      同构换值 → 两条并存
    pending    判据不确定／与既有记忆冲突 → 新条以 candidate 挂起，人工确认后生效

判据（任一不满足打印 FAIL、退出码 1）：
- P-1 每条变更句都必须在库里（不许消失）；
- P-2 走 pending 的，必须**有可裁决的确认块**（不能是死端 candidate），且确认之后
  旧条转 superseded、新条转 active；
- P-3 终态必须"改口生效"：对该对象提问，注入的记忆里出现**新值**且不再出现旧值。

跑法（离线、零出站、默认写 D:/tmp）：
    python scripts/live_supersede_probe.py                       # 用当前配置的嵌入档
    HIPPOCAMPUS_EMBEDDING_MODEL="onnx:Xenova/bge-small-zh-v1.5" python scripts/live_supersede_probe.py
    python scripts/live_supersede_probe.py --json D:/tmp/probe.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# force-utf8 shim：Windows 控制台默认代码页打不出中文/箭头会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# (旧句, 新句, 提问句)：覆盖 supersede 两支（数值变更／极性翻转）、admit 一支
# （同构换值），以及"改写型变更"（机械判据拿不准 → 必须落到挂起确认而不是丢）。
PAIRS: list[tuple[str, str, str]] = [
    ("我的期望城市是北京", "我的期望城市是杭州", "我现在想去哪个城市工作？"),
    ("我喜欢 42 码的鞋", "我喜欢 40 码的鞋", "我穿几码的鞋？"),
    ("我喜欢深色主题", "我不喜欢深色主题", "我喜欢什么样的界面主题？"),
    ("我用 VS Code 写代码", "我用 Vim 写代码", "我平时用什么写代码？"),
    ("我每周做两次运动", "我每周做三次运动", "我每周做几次运动？"),
    ("我接受远程办公", "我不接受远程办公", "我接受远程办公吗？"),
    ("我习惯用 Chrome 浏览器", "我习惯用 Firefox 浏览器", "我常用什么浏览器？"),
]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真机变更句实测（supersede 生效验证）")
    ap.add_argument("--home", default="D:/tmp/hc7/supersede-live", help="数据根（默认 D:/tmp/hc7/supersede-live）")
    ap.add_argument("--account", default="supersede-live")
    ap.add_argument("--settle", type=float, default=0.8, help="写旧句后等待向量索引落盘的秒数")
    ap.add_argument("--json", help="把结果写到该路径")
    args = ap.parse_args(argv)

    from hippocampus.core import MemoryCore, Scope
    from hippocampus.memory import config as mem_config
    from hippocampus.memory import dedup, retrieval

    home = Path(args.home)
    if home.exists():
        shutil.rmtree(home)  # 测量纪律：每次从 fresh home 出发

    model = mem_config.get_embedding_config()["model"]
    embed_fn = retrieval._resolve_embedding_function(model)  # noqa: SLF001
    if embed_fn is None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        embed_fn = ONNXMiniLM_L6_V2()
        effective = "onnx_mini_lm_l6_v2（配置串未命中 → 回退档）"
    else:
        effective = getattr(embed_fn, "name", lambda: model)()

    print("== 真机变更句实测（七轮 T5）==")
    print(f"配置档: {model}   实际生效嵌入: {effective}")

    core = MemoryCore(home=home)
    rows: list[dict] = []
    failures: list[str] = []
    try:
        for i, (old, new, question) in enumerate(PAIRS):
            # 每对**独立账户**：同一账户里堆多条同主题记忆时，神经档会把它们彼此靠拢
            # （实测 bge-zh 下 Chrome↔Firefox 与 VS Code↔Vim 互 sim ≥0.93），
            # 最近邻就不再是"本对的旧句"，测不出 supersede 分支。分账户才是干净归因。
            account = f"{args.account}-{i}"
            failures_before = len(failures)
            s_old = Scope(account=account, session="s-1", source="user")
            s_new = Scope(account=account, session="s-2", source="user")
            core.write(s_old, old, kind="preference")
            threshold = float(core.active_params(s_old).get("semantic_dup_threshold", 0.85))
            time.sleep(args.settle)
            result = core.write(s_new, new, kind="preference")

            sim = _cosine(embed_fn([old])[0], embed_fn([new])[0])
            expect = dedup.classify_dup_action(old, new)
            statuses = {it.content: it.status for it in core.list_memories(
                s_new, limit=100, status=None, include_shadow=True)}
            route = "supersede" if result.superseded else ("pending" if result.pending else "admit")
            row = {
                "old": old, "new": new, "sim": round(sim, 4), "threshold": round(threshold, 3),
                "dedup_would_hit": sim >= threshold, "classifier": expect, "route": route,
                "old_status": statuses.get(old), "new_status": statuses.get(new),
            }

            # P-1 变更句不许消失
            if row["new_status"] is None:
                failures.append(f"P-1 「{new}」被吞（库里查不到）")
            # P-2 挂起必须可裁决，裁决后必须转正
            if row["new_status"] == "candidate":
                pending = core.pending(s_new)
                row["pending_blocks"] = len(pending)
                if not pending:
                    failures.append(f"P-2 「{new}」是 candidate 却没有确认块（死端挂起）")
                else:
                    candidate = next((p for p in pending if p["is_new"]), None)
                    outcome = core.confirm(s_new, f"确认{candidate['num']}") if candidate else None
                    row["confirm_decision"] = getattr(outcome, "decision", None)
            # 终态核对
            final = {it.content: it.status for it in core.list_memories(
                s_new, limit=100, status=None, include_shadow=True)}
            row["final_old"], row["final_new"] = final.get(old), final.get(new)
            if row["route"] == "supersede" and row["final_old"] != "superseded":
                failures.append(f"P-2 声称 supersede 但旧条状态是 {row['final_old']}：「{old}」")
            if row["final_new"] != "active":
                failures.append(f"P-2 变更句终态不是 active（{row['final_new']}）：「{new}」")
            # P-3 终态语义：supersede/pending 两条路必须"改口生效"（取新弃旧）；
            # admit 只要求**新值可被召回**（两条并存是库里状态，注入是 top-8 排序结果，
            # 旧值排不进来属正常，不是缺陷）。
            injection = core.inject_finalize(s_new, question)
            contents = [it.content for it in injection.items]
            has_new = any(new in c for c in contents)
            has_old = any(old in c for c in contents)
            if route == "admit":
                row["p3_ok"] = has_new
            else:
                row["p3_ok"] = has_new and not has_old
            row["injected"] = contents
            if not row["p3_ok"]:
                failures.append(f"P-3 提问「{question}」注入不符（分支={route}，新={has_new} 旧={has_old}）：{contents}")

            rows.append(row)
            print(f"\n  「{old}」→「{new}」")
            print(f"    对内 sim={row['sim']} vs 该档阈值 {row['threshold']}（{'会命中' if row['dedup_would_hit'] else '不命中'}）"
                  f"   机械判据={expect}   实走分支={route}")
            print(f"    终态：旧={row['final_old']} 新={row['final_new']}   注入含新值={row['p3_ok']}")
            print("    [PASS]" if len(failures) == failures_before else "    [FAIL]")
    finally:
        core.close()

    print("\n== 汇总 ==")
    print(f"档位: {effective}")
    by_route: dict[str, int] = {}
    for r in rows:
        by_route[r["route"]] = by_route.get(r["route"], 0) + 1
    print(f"变更句 {len(rows)} 对，分支分布：{by_route}")
    print(f"被吞/死端/改口未生效 共 {len(failures)} 项")
    for f in failures:
        print(f"  - {f}")
    ok = not failures
    print("结论：" + ("真机变更句全部存活、可裁决、改口后注入取到新值 → supersede 链路生效。" if ok else "存在失败项，见上。"))
    if args.json:
        payload = {"model": model, "effective_embedding": effective, "pairs": rows,
                   "failures": failures, "pass": ok}
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
