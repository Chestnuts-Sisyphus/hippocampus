"""检索阈值标定：用**合成示例数据 + 标注查询集**扫出「证据线」等参数。

为什么要有这个脚本（验收 A32）：
- 相似度是**相对量**，换嵌入档就换量纲；把阈值写死在代码里就是埋雷。
  本项目把"档位 → 参数"做成显式表（memory/calibration.py），本脚本负责**量出来**。

跑法（离线，零网络）：
    python scripts/calibrate.py                 # 扫内置档
    python scripts/calibrate.py --model onnx:bge-small-zh-v1.5

输出：每档的分数分布（正/负样本分位数）+ 建议 `answer_floor`（取"负样本最大值"与
"正样本最小值"之间，优先保精确率：宁可说不知道）。结果写进 docs/embedding.md。

诚实边界：样本为合成数据（含已知真值），只证"方法可复现"，不构成效果结论。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hippocampus.core import MemoryCore, Scope  # noqa: E402
from hippocampus.seed import seed  # noqa: E402

ACCOUNT = "calib"

# 正样本：查询 → 必须命中的记忆（子串）
POSITIVE: dict[str, list[str]] = {
    "我投简历有什么要求": ["简历投递前必须先过一遍错别字"],
    "我可以接受出差吗": ["我不投需要长期出差的岗位"],
    "我想去哪个城市": ["我的期望城市是杭州"],
    "我的方向是什么": ["我的目标岗位方向是后端开发与基础设施"],
    "周三晚上做什么": ["我每周三晚上固定留给项目开发"],
    "远程岗位我可以投吗": ["我只看允许远程的岗位"],
}

# 负样本：这些问题在库里**没有对应记忆**，正确行为是"说不知道"
NEGATIVE: list[str] = [
    "我今天中午吃了什么",
    "帮我写一首关于秋天的诗",
    "明天天气怎么样",
    "1+1 等于几",
    "推荐一部电影",
]

# ----------------------------------------------------------------------
# 英文合成标定集（八轮 V6）：与中文集**主题一一对应**，量法完全相同，
# 这样英文档与中文档的分数分布才可比。为什么单独一套：`UNCALIBRATED_TIERS`
# 那条缺口的成因就是"标定集是中文合成样本，用它量英文档没有意义"。
# ----------------------------------------------------------------------
EN_MEMORIES: list[str] = [
    "Proofread the resume before submitting it anywhere",
    "I do not apply to jobs that require long-term business travel",
    "My preferred city to work in is Hangzhou",
    "My target role direction is backend engineering and infrastructure",
    "Wednesday evening is reserved for project development",
    "I only look at positions that allow remote work",
]

EN_POSITIVE: dict[str, list[str]] = {
    "What must I do before I submit a resume": ["Proofread the resume"],
    "Can I take a job with a lot of travel": ["long-term business travel"],
    "Which city do I want to work in": ["preferred city to work in is Hangzhou"],
    "What engineering direction am I aiming at": ["backend engineering and infrastructure"],
    "What is my Wednesday evening kept for": ["reserved for project development"],
    "Is a remote-friendly position acceptable": ["allow remote work"],
}

EN_NEGATIVE: list[str] = [
    "What did I have for lunch today",
    "Write me a poem about autumn",
    "Will it rain tomorrow",
    "What is twelve plus five",
    "Recommend me a movie",
]



# force-utf8 shim：Windows 控制台默认代码页（CI 里是 cp1252）无法编码 ✓ 等字符，会让"打印"把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def _top_score(core: MemoryCore, account: str, query: str) -> tuple[float, list[str]]:
    """每条查询用**独立会话**：会话实体兜底（session_entity_fallback）会把同会话
    前几轮的实体带进来，混在一起就测不出"这条查询本身命中多少"。"""
    scope = Scope(account=account, session=f"calib-{abs(hash(query)) % 10**6}")
    result = core.search(scope, query, limit=5)
    score = max((i.score for i in result.items), default=0.0)
    return score, [i.content for i in result.items]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="", help="覆盖嵌入档（如 onnx:bge-small-zh-v1.5）")
    ap.add_argument(
        "--lang",
        choices=("zh", "en"),
        default="zh",
        help="标定集语言（八轮 V6）：英文档必须用英文合成集量，中文集量英文档没有意义",
    )
    ap.add_argument("--json", help="把结果写到该路径")
    args = ap.parse_args()

    positives = EN_POSITIVE if args.lang == "en" else POSITIVE
    negatives = EN_NEGATIVE if args.lang == "en" else NEGATIVE

    if args.model:
        import os

        os.environ["HIPPOCAMPUS_EMBEDDING_MODEL"] = args.model

    from hippocampus.memory import config as mem_config

    model = mem_config.get_embedding_config()["model"]
    home = Path(tempfile.mkdtemp(prefix="hippo_calib_"))
    core = MemoryCore(home=home)
    seed_scope = Scope(account=ACCOUNT, session="seed")
    if args.lang == "en":
        # 英文集不走 seed()（那是中文合成清单）：逐条写进**同一账户**，其余量法完全一致
        for i, content in enumerate(EN_MEMORIES):
            core.write(Scope(account=ACCOUNT, session=f"seed-en-{i}"), content, kind="preference")
    else:
        seed(core, seed_scope)

    pos_scores: list[float] = []
    neg_scores: list[float] = []
    pos_rows: list[dict] = []
    neg_rows: list[dict] = []

    for query, expect in positives.items():
        score, contents = _top_score(core, ACCOUNT, query)
        hit = any(any(e in c for c in contents) for e in expect)
        pos_scores.append(score)
        pos_rows.append({"query": query, "top": round(score, 4), "hit": hit, "expect": expect})

    for query in negatives:
        score, _ = _top_score(core, ACCOUNT, query)
        neg_scores.append(score)
        neg_rows.append({"query": query, "top": round(score, 4)})

    pos_min = min(pos_scores) if pos_scores else 0.0
    neg_max = max(neg_scores) if neg_scores else 0.0
    if pos_min > neg_max:
        floor = round((pos_min + neg_max) / 2, 3)
        separable = True
    else:
        floor = round(neg_max + 0.01, 3)   # 不可分时优先保精确率
        separable = False

    report = {
        "model": model,
        "lang": args.lang,
        "set_sizes": {"positive": len(positives), "negative": len(negatives)},
        "positive": {
            "n": len(pos_scores),
            "min": round(pos_min, 4),
            "median": round(statistics.median(pos_scores), 4) if pos_scores else 0,
            "max": round(max(pos_scores), 4) if pos_scores else 0,
            "recall_at_absolute_floor": sum(1 for r in pos_rows if r["hit"]) / max(len(pos_rows), 1),
            "rows": pos_rows,
        },
        "negative": {
            "n": len(neg_scores),
            "min": round(min(neg_scores), 4) if neg_scores else 0,
            "median": round(statistics.median(neg_scores), 4) if neg_scores else 0,
            "max": round(neg_max, 4),
            "rows": neg_rows,
        },
        "separable": separable,
        "suggested_answer_floor": floor,
        "note": (
            "样本为合成示例数据（含已知真值），只证方法可复现；"
            "answer_floor 取正负样本之间且偏向保精确率（宁说不知道）"
        ),
    }
    core.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
