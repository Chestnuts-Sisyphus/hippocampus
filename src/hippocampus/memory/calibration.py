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
    # 推荐档：bge-small-zh-v1.5（中文神经嵌入）。标定值来自前身标定报告
    # `_eval_threshold_calibration_hc0802.md`（注入 0.50／重述 0.58）。
    "onnx:bge-small-zh-v1.5": {
        "absolute_floor": 0.50,
        "cliff_gap_min": 0.08,
        "cliff_ratio": 0.30,
        "restate_threshold": 0.58,
        "semantic_dup_threshold": 0.85,
        "answer_floor": 0.55,
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


def params_for_tier(model: str) -> dict[str, Any]:
    """取某档的参数；未知档 → 内置档参数（并让调用方自行提示）。"""
    return dict(TIER_PARAMS.get(model, TIER_PARAMS[DEFAULT_TIER]))


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


__all__ = ["DEFAULT_TIER", "TIER_PARAMS", "apply_tier_params", "has_tier", "params_for_tier"]
