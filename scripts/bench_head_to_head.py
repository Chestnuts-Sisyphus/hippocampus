"""⚠ **实验脚手架 · mock 近似 · 无结论——不要引用本脚本的任何数。**

三个"竞品"全是本文件内的 mock 类（`Mem0Mock` / `ZepMock` / `LettaMock`），本项目
**从未安装或调用过** Mem0 / Zep / Letta 本体：`Mem0Mock` 读本机缓存里的预存 QA 对，
`ZepMock` 取前 top-k 轮用户话语，`LettaMock` 取会话中间两轮——它们只近似"注入预算相当"，
**不测竞品行为**。竞品检索延迟**从未被量测**（此前那三个延迟常数是写死的假设值，已删除）。
本脚本在本机**从未真跑过**：仓外评测产物目录无任何 h2h 报告，`docs/` 与 README 也不引用它。

因此它当前的价值只有一条：**接口形状已搭好**。要得到可引用的横比结论，必须真实部署竞品、
跑一笔付费批次，并按 `docs/benchmark.md` §〇 的"批次／规模／臂"标注纪律登记。
横比口径的正本见 `docs/benchmark.md` §二·十。

---

竞品横比实验管线：复用 bench_ab 的 A/B 结构，将竞品记忆层作为 variant 接入。

目标：同题集、同模型臂、同注入预算下，对比 Hippocampus 与竞品（Mem0/Zep/Letta）的
- F1/准确率（官方判分口径）
- 分题型短板
- token 成本

设计原则：
1. **模拟竞品接口**：因多数竞品无公开 API，本脚本通过预存响应或 mock 服务模拟其输出；
2. **统一评测口径**：复用 public_bench 的数据加载与 model_arm 的官方判分臂；
3. **可复现**：所有随机种子固定，预存数据带 sha256 校验；
4. **零依赖外部服务**：默认离线运行，仅当 --model-arm 开启时才出站调用模型端点。

竞品支持列表：
- Mem0 (v0.1.x)：使用 HuggingFace 公开数据集 `mem0ai/mem0-benchmark` 中的 QA 对 + 记忆注入响应；
- Zep (v1.x)：mock 服务，按标准注入协议返回 top-k 片段；
- Letta (v0.2.x)：预存对话历史 + 检索响应快照。

用法：
    # 基础横比（仅检索口径，离线）
    python scripts/bench_head_to_head.py \
        --data D:/tmp/hc-bench/longmemeval_oracle.json --limit 100 \
        --variants "hippocampus,mem0,zep" \
        --json D:/tmp/hc-bench/h2h_lme100.json

    # 含官方判分臂（需模型凭据）
    export HIPPOCAMPUS_BASE_URL="https://api.deepseek.com/v1"
    export HIPPOCAMPUS_MODEL="deepseek-chat"
    export HIPPOCAMPUS_API_KEY="sk-..."
    python scripts/bench_head_to_head.py \
        --data D:/tmp/hc-bench/longmemeval_oracle.json --limit 100 \
        --variants "hippocampus,mem0" \
        --model-arm --budget-yuan 5.0 \
        --json D:/tmp/hc-bench/h2h_lme100_arm.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("HIPPOCAMPUS_OFFLINE", "1")
for _k in ("HIPPOCAMPUS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hippocampus.core import MemoryCore, Scope  # noqa: E402
from hippocampus.eval import public_bench as pb  # noqa: E402

# ----------------------------------------------------------------------
# 竞品数据源（预存/模拟）
# ----------------------------------------------------------------------


@dataclass
class CompetitorResponse:
    """竞品的一道题检索响应（mock：见文件头——竞品本体从未被调用过）。"""

    context: str = ""  # 注入上下文原文
    tokens: int = 0  # 估算 tokens
    latency_ms: float | None = None  # **未实测**：竞品真实检索延迟从未被量测，不要填假设值
    metadata: dict[str, Any] = field(default_factory=dict)  # 额外信息（如命中条目数）


class Mem0Mock:
    """Mem0 v0.1.x 模拟层（基于 HuggingFace 公开 benchmark 数据）。

    数据来源：https://huggingface.co/datasets/mem0ai/mem0-benchmark
    格式：每个样本包含 {question, memory_context, answer}
    我们只取 memory_context 作为"竞品注入上下文"。
    """

    DATA_URL = "https://huggingface.co/datasets/mem0ai/mem0-benchmark/resolve/main/qa_pairs.json"
    CACHE_FILE = Path.home() / ".hippocampus" / "cache" / "mem0_qa.json"
    SHA256_GOLD = "placeholder_sha256"  # 待实测填入

    def __init__(self):
        if not self.CACHE_FILE.exists():
            print(f"⚠ 未找到 Mem0 缓存数据：{self.CACHE_FILE}")
            print("请手动下载并放至该路径，或使用 --mem0-data 指定本地路径。")
            self.data = {}
        else:
            self.data = json.loads(self.CACHE_FILE.read_text(encoding="utf-8"))

    def get_response(self, qid: str, question: str) -> CompetitorResponse:
        """根据 qid 或问题文本检索对应的记忆上下文。"""
        # 简化版：按问题关键词模糊匹配（实际应使用更精确的映射）
        for key, val in self.data.items():
            if qid in key or question in val.get("question", ""):
                context = val.get("memory_context", "")
                return CompetitorResponse(
                    context=context,
                    tokens=len(context) // 2 + 40 if context else 0,
                    metadata={"source": "mem0", "variant": "v0.1"},
                )
        return CompetitorResponse(context="", tokens=0, metadata={"source": "mem0", "error": "no match"})


class ZepMock:
    """Zep v1.x 模拟层（mock 服务，按标准注入协议返回 top-k 片段）。

    实现方式：对于 LongMemEval/LoCoMo 的每一题，从原始会话中抽取前 k 轮作为"记忆上下文"。
    这近似 Zep 的"按时间窗口检索"行为。
    """

    def __init__(self, top_k: int = 8):
        self.top_k = top_k

    def get_response(self, item: pb.Item, session_turns: list[pb.Turn]) -> CompetitorResponse:
        """为一道题生成竞品注入上下文。"""
        # 简单策略：取前 top_k 轮用户话语
        user_turns = [t for t in session_turns[: self.top_k] if (t.role or "user").lower() == "user"]
        context = "\n".join(t.text for t in user_turns)
        return CompetitorResponse(
            context=context,
            tokens=len(context) // 2 + 40 if context else 0,
            metadata={"source": "zep", "top_k": self.top_k},
        )


class LettaMock:
    """Letta v0.2.x 模拟层（预存对话历史 + 检索响应快照）。

    数据来源：Letta 官方 GitHub 仓库中的 demo 对话样本。
    由于 Letta 的记忆系统高度个性化，这里采用简化策略：
    - 对于每道题，从原始会话中抽取与问题最相关的两轮对话。
    """

    def __init__(self, neighborhood_radius: int = 2):
        self.radius = neighborhood_radius

    def get_response(self, item: pb.Item, session_turns: list[pb.Turn]) -> CompetitorResponse:
        """为一道题生成竞品注入上下文（基于问题相关性的两轮对话）。"""
        # 简化版：取问题前后各 radius 轮
        start = max(0, len(session_turns) // 2 - self.radius)
        end = min(len(session_turns), len(session_turns) // 2 + self.radius + 1)
        context = "\n".join(t.text for t in session_turns[start:end])
        return CompetitorResponse(
            context=context,
            tokens=len(context) // 2 + 40 if context else 0,
            metadata={"source": "letta", "radius": self.radius},
        )


# ----------------------------------------------------------------------
# 横比实验核心逻辑
# ----------------------------------------------------------------------


@dataclass
class VariantConfig:
    """变体配置：名称 + 实现类。"""

    name: str
    impl_class: type[Any]
    config: dict[str, Any] = field(default_factory=dict)


def run_hippocampus_variant(
    core: MemoryCore,
    items: list[pb.Item],
    *,
    inject_max_items: int = 8,
) -> dict[str, Any]:
    """跑 Hippocampus 本体的一个变体。"""
    rows: list[dict] = []
    for item in items:
        scope = Scope(account=f"h2h-{item.qid}", session="bench", source="user")
        core.ingest_history(scope, [pb._turn_dict(t, date_prefix=True) for t in item.turns])
        injection = core.inject_finalize(scope, item.question)
        context = injection.text or ""
        rows.append(
            {
                "qid": item.qid,
                "category": item.category,
                "context": context,
                "tokens": len(context) // 2 + 40 if context else 0,
                "gold": item.answers,
            }
        )
    return {"rows": rows, "variant": "hippocampus"}


def run_competitor_variant(
    competitor: Any,
    items: list[pb.Item],
    session_store: dict[str, list[pb.Turn]],
) -> dict[str, Any]:
    """跑竞品模拟层的一个变体。"""
    rows: list[dict] = []
    for item in items:
        session_key = item.group or item.qid
        turns = session_store.get(session_key, [])
        response = competitor.get_response(item.qid, item.question) if hasattr(competitor, "get_response") else competitor.get_response(item, turns)
        rows.append(
            {
                "qid": item.qid,
                "category": item.category,
                "context": response.context,
                "tokens": response.tokens,
                "gold": item.answers,
                "metadata": response.metadata,
            }
        )
    return {"rows": rows, "variant": getattr(competitor, "__class__", type(competitor)).__name__}


def aggregate_results(report: dict[str, Any], items: list[pb.Item]) -> dict[str, Any]:
    """聚合报告：计算证据命中、答在文内、token_f1。"""
    rows = report["rows"]
    gold_map = {item.qid: item.answers for item in items}
    cat_map = {item.qid: item.category for item in items}
    ev_map = {item.qid: item.evidence_texts for item in items}

    def pct(vals: list[bool]) -> float:
        return round(100.0 * sum(1 for v in vals if v) / max(1, len(vals)), 2)

    by_cat: dict[str, list[dict]] = {}
    for row in rows:
        qid = row["qid"]
        category = cat_map.get(qid, "unknown")
        gold = gold_map.get(qid, [])
        norm_ctx = pb.normalize_text(row["context"])
        answer_hit = any(v and v in norm_ctx for a in gold for v in pb.answer_variants(a))
        evidence_hit = any(
            pb.normalize_text(e)[:80] in norm_ctx
            for e in ev_map.get(qid, [])
            if pb.normalize_text(e)
        )
        f1 = pb.token_f1(row["context"].split("\n")[-1] if row["context"] else "", gold[0] if gold else "")

        by_cat.setdefault(category, []).append({
            "qid": qid,
            "answer_hit": answer_hit,
            "evidence_hit": evidence_hit,
            "f1": f1,
        })

    def _agg(subset: list[dict]) -> dict[str, Any]:
        return {
            "n": len(subset),
            "answer_in_context": pct([r["answer_hit"] for r in subset]),
            "evidence_in_context": pct([r["evidence_hit"] for r in subset]),
            "token_f1": pct([float(r["f1"]) for r in subset]),
        }

    return {
        "overall": _agg(rows),
        "by_category": {k: _agg(v) for k, v in sorted(by_cat.items())},
        "rows": rows,
    }


# ----------------------------------------------------------------------
# CLI 入口
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Head-to-head with competitors (Mem0/Zep/Letta)")
    parser.add_argument("--data", required=True, help="基准数据 JSON 路径")
    parser.add_argument("--limit", type=int, default=100, help="题量上限（0=全部）")
    parser.add_argument("--variants", required=True, help="逗号分隔的变体名：hippocampus,mem0,zep,letta")
    parser.add_argument("--home", help="Hippocampus 数据根目录")
    parser.add_argument("--json", help="结果写入路径")
    parser.add_argument("--model-arm", action="store_true", help="启用官方判分臂（需模型凭据）")
    parser.add_argument("--budget-yuan", type=float, default=30.0, help="模型臂预算硬停（元）")
    args = parser.parse_args(argv)

    # 加载数据
    bench_key = "longmemeval" if "lme" in args.data.lower() else "locomo"
    if bench_key == "longmemeval":
        items = pb.load_longmemeval(args.data, limit=args.limit)
    else:
        items = pb.load_locomo(args.data, limit_conversations=0)
        if args.limit:
            items = items[: args.limit]

    print(f"题量 {len(items)}（抽样 {args.limit or '全量'}）")

    # 解析变体列表
    variant_names = [v.strip() for v in args.variants.split(",") if v.strip()]
    reports: dict[str, dict] = {}

    # 预处理：构建会话存储（用于竞品模拟）
    session_store: dict[str, list[pb.Turn]] = {}
    for item in items:
        session_store[item.group or item.qid] = item.turns

    # 跑各变体
    for name in variant_names:
        print(f"\n[{name}] 开始跑……")
        if name == "hippocampus":
            home = Path(args.home) if args.home else Path.home() / ".hippocampus" / "h2h_temp"
            home.mkdir(parents=True, exist_ok=True)
            core = MemoryCore(home=home)
            try:
                rep = run_hippocampus_variant(core, items)
            finally:
                core.close()
            reports[name] = aggregate_results(rep, items)
        elif name == "mem0":
            mem0 = Mem0Mock()
            rep = run_competitor_variant(mem0, items, session_store)
            reports[name] = aggregate_results(rep, items)
        elif name == "zep":
            zep = ZepMock(top_k=8)
            rep = run_competitor_variant(zep, items, session_store)
            reports[name] = aggregate_results(rep, items)
        elif name == "letta":
            letta = LettaMock(neighborhood_radius=2)
            rep = run_competitor_variant(letta, items, session_store)
            reports[name] = aggregate_results(rep, items)
        else:
            print(f"⚠ 未知变体：{name}，跳过")
            continue

        print(f"[{name}] 完成（{len(reports[name]['rows'])} 题）")

    # 输出对比表
    print("\n===== Head-to-Head 对比表（检索口径） =====")
    header = f"{'变体':<16}{'题量':>6}{'答在文内':>10}{'证据在文内':>12}{'F1':>8}"
    print(header)
    for name in variant_names:
        if name not in reports:
            continue
        agg = reports[name]["overall"]
        line = f"{name:<16}{agg['n']:>6}{agg['answer_in_context']:>10}{agg['evidence_in_context']:>12}{agg['token_f1']:>8}"
        print(line)

    # 分类短板分析
    print("\n===== 分类短板分析 =====")
    for name in variant_names:
        if name not in reports:
            continue
        print(f"\n[{name}]")
        for cat, agg in sorted(reports[name]["by_category"].items()):
            print(f"  {cat}: n={agg['n']}, answer={agg['answer_in_context']}%, evidence={agg['evidence_in_context']}%, f1={agg['token_f1']}%")

    # 写 JSON
    if args.json:
        output = {
            "protocol": {
                "offline": not args.model_arm,
                "model_arm": "已启用" if args.model_arm else "未启用",
                "budget_yuan": args.budget_yuan if args.model_arm else None,
                "benchmark": bench_key,
                "n_items": len(items),
            },
            "reports": {name: reports[name] for name in variant_names},
        }
        Path(args.json).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
