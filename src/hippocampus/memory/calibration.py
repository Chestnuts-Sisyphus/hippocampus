"""嵌入档 → 检索参数标定表（验收 A32／A37）。

为什么必须分档标定：相似度**量纲随嵌入模型变**。神经嵌入（bge／MiniLM）在中文短句上
的相关/不相关分界约 0.5 附近；而本项目的内置档 `builtin-hash` 是词法级表征，
同源改写给 0.2–0.5、不相关给 0–0.15——拿神经档的 0.3 当地板会把相关条全滤掉。

所以：**档位 → 参数**做成显式表，"当前生效值"由 `apply_tier_params()` 幂等写进
活跃参数快照（可人工改，改了就生效）。测量方法见 `scripts/calibrate.py`
与 `docs/embedding.md`（含样本边界声明）。

诚实边界：表里的数字来自**合成示例数据 + 固定查询集**的扫描结果，只证"方法可复现"；
效果结论只在私有库运行史上另行单列（交接正本 §1.7 纪律）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

# 每个档位一套参数。键名与 param_snapshots 里的 params JSON 一致。
TIER_PARAMS: dict[str, dict[str, Any]] = {
    # 内置档：词法级（零下载）。地板低、断崖阈值低（词法分差本来就更小）。
    "builtin-hash": {
        "absolute_floor": 0.10,
        "cliff_gap_min": 0.06,
        "cliff_ratio": 0.25,
        "restate_threshold": 0.40,
        "semantic_dup_threshold": 0.80,
        # 证据线：检索最高分低于它就不算"有依据"（宁说不知道，不硬答）。
        # 实测（scripts/calibrate.py，合成示例数据）：正样本 0.135–0.435（中位 0.321），
        # 负样本 0.000–0.160（中位 0.131）——**两分布有重叠，单一分数不可分**。
        # 取 0.17 = 负样本最大值 + 0.01：负样本 0/5 误入，正样本 5/6 命中
        # （丢的那条是"我可以接受出差吗" 0.135，词法档的真实短板，边界写在 docs/embedding.md）。
        "answer_floor": 0.17,
    },
    # 推荐档：bge-small-zh-v1.5（中文神经嵌入）。
    # **表键必须与真实配置串逐字相等**（含 HF 组织前缀 `Xenova/`）：裸名
    # `onnx:bge-small-zh-v1.5` 在 embedding_models 里对不上本地模型目录，会静默回退
    # MiniLM（实测：两下载端点均 401 → 回退，中文相似度虚高到负样本 0.75），
    # 键名一错就整档参数不生效。
    # answer_floor 本轮重标定（`scripts/calibrate.py --model onnx:Xenova/bge-small-zh-v1.5`，
    # 2026-09-19 本机实测）：正样本 0.5255–0.6293（中位 0.5745）／负样本 0.2568–0.4043
    # （中位 0.3488）→ **两分布可分**，取中点 0.465。旧值 0.55 来自前身标定报告，
    # 高于本轮实测正样本最小值（会把 0.5255 那条真命中判成"无依据"）。
    # absolute_floor 0.50 仍在正样本最小值之下（召回优先），沿用；
    # cliff／restate／semantic_dup 本轮**未量出**，仍沿用前身口径（0.08/0.30/0.58/0.85）。
    "onnx:Xenova/bge-small-zh-v1.5": {
        "absolute_floor": 0.50,
        "cliff_gap_min": 0.08,
        "cliff_ratio": 0.30,
        "restate_threshold": 0.58,
        "semantic_dup_threshold": 0.85,
        "answer_floor": 0.465,
    },
    # chromadb 内置档（英文 MiniLM）：中文虚高已在标定中证明，仅作回退选项。
    "onnx_mini_lm_l6_v2": {
        "absolute_floor": 0.30,
        "cliff_gap_min": 0.10,
        "cliff_ratio": 0.30,
        "restate_threshold": 0.80,
        "semantic_dup_threshold": 0.85,
        "answer_floor": 0.35,
    },
}

DEFAULT_TIER = "builtin-hash"

# **已声明的标定缺口**：文档承诺支持、但本仓库标定表没有该档数值的档。
# 落进这里的档会用内置档参数（词法量纲，证据线偏松），并由 `apply_tier_params`
# 打一条 stderr 告警——不静默。为什么没有数值：`scripts/calibrate.py` 的标定集是
# **中文**合成样本，用它量英文档没有意义；英文档的真实结论在 `docs/benchmark.md`
# （LoCoMo 证据命中 36.7%→45.7%，检索口径、非本表标定产物）。
# 要消解这条缺口：给一个英文合成标定集，重标定后把键移进 `TIER_PARAMS`。
UNCALIBRATED_TIERS: frozenset[str] = frozenset({"onnx:Xenova/bge-small-en-v1.5"})


def params_for_tier(model: str) -> dict[str, Any]:
    """取某档的参数；未知档 → 内置档参数（提示由 `apply_tier_params` 负责，见下）。"""
    return dict(TIER_PARAMS.get(model, TIER_PARAMS[DEFAULT_TIER]))


# 同进程内每个档位只提示一次（这条走的是启动路径，重复打会淹掉真正的告警）
_WARNED_TIERS: set[str] = set()


def _warn_tier_fallback(model: str) -> None:
    if model in _WARNED_TIERS:
        return
    _WARNED_TIERS.add(model)
    if model in UNCALIBRATED_TIERS:
        detail = "该档已登记为**未标定缺口**（原因与消解方式见 UNCALIBRATED_TIERS 注释）"
    else:
        detail = "档位表里没有这一键，参数按内置档回落——**换嵌入档没换量纲，检索阈值会错位**"
    print(
        f"[warning] 嵌入档 {model!r} 无标定值：{detail}；"
        f"请用 `python scripts/calibrate.py --model {model}` 量出来后写进 memory/calibration.py。",
        file=sys.stderr,
    )


def has_tier(model: str) -> bool:
    return model in TIER_PARAMS


def apply_tier_params(conn: sqlite3.Connection, model: str, *, force: bool = False) -> dict[str, Any]:
    """把档位参数幂等写进活跃参数快照。

    覆盖策略（三态，避免"每次启动都清掉人工调参"）：
    - **首次应用**（快照里还没有 `embedding_tier`）→ 覆盖：库里的通用默认值按档位重写；
    - **同档重复进入** → 只补缺：人工调过的键保留；
    - **换档**（`embedding_tier` 与当前档不同）→ 覆盖：换嵌入档必须重新标定，
      否则拿旧量纲的阈值去比新量纲的相似度，检索会静默失真。
    """
    from hippocampus.memory import database as db

    row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
    if not row:
        db.seed_initial_snapshot(conn)
        row = conn.execute("SELECT id, params FROM param_snapshots WHERE is_active=1 LIMIT 1").fetchone()
        if not row:
            return {}
    try:
        params = json.loads(row["params"])
    except (json.JSONDecodeError, TypeError, KeyError):
        params = {}
    prev_tier = params.get("embedding_tier")
    overwrite = force or prev_tier is None or prev_tier != model
    if not has_tier(model):
        _warn_tier_fallback(model)
    tier = params_for_tier(model)
    changed = False
    for key, value in tier.items():
        if (overwrite or key not in params) and params.get(key) != value:
            params[key] = value
            changed = True
    if params.get("embedding_tier") != model:
        params["embedding_tier"] = model
        changed = True
    if changed:
        conn.execute(
            "UPDATE param_snapshots SET params=? WHERE id=?",
            (json.dumps(params, ensure_ascii=False), row["id"]),
        )
        conn.commit()
    return params


__all__ = ["DEFAULT_TIER", "TIER_PARAMS", "UNCALIBRATED_TIERS", "apply_tier_params", "has_tier", "params_for_tier"]
