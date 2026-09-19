"""七轮 T1 守护：嵌入档位表 ↔ 配置串 ↔ 文档 三者同源（防"换型静默失效"复发）。

踩坑实证（2026-09-19，见 `docs/embedding.md` §七轮换型实测·裸名陷阱）：档位表键与文档
都写裸名 `onnx:bge-small-zh-v1.5`，而真正能加载的配置串带 HF 组织前缀
`onnx:Xenova/bge-small-zh-v1.5`。两处同时错时：

1. `embedding_models` 按裸名去下载 → 两端点 401 → **静默回退 MiniLM**（中文虚高档）；
2. `calibration.TIER_PARAMS` 查不到该键 → `apply_tier_params()` 落回内置档参数 →
   **换了模型却没换量纲**，检索阈值全部错位。

所以把这些不变式钉成测试：任何一处漂移都会在 CI 红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hippocampus.memory import calibration, config, embedding_models

REPO_ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_DOC = REPO_ROOT / "docs" / "embedding.md"

ZH_TIER_KEY = "onnx:Xenova/bge-small-zh-v1.5"


def _onnx_tier_repos() -> list[str]:
    return [k.split(":", 1)[1] for k in calibration.TIER_PARAMS if k.startswith("onnx:")]


def test_onnx_tier_keys_are_org_prefixed_repo_paths():
    """档位表里的 onnx 档必须是 `org/name` 形态：裸名不是合法 HF 仓库路径。"""
    repos = _onnx_tier_repos()
    assert repos, "档位表里应有至少一个 onnx 档"
    for repo in repos:
        assert "/" in repo, f"档位表键 {repo!r} 缺 HF 组织前缀（会静默回退 MiniLM）"
        # 仓库名必须能过加载器白名单（路径穿越/主机注入那道闸）
        assert embedding_models._safe_repo(repo) == repo  # noqa: SLF001


def test_default_tiers_resolve_without_fallback():
    """默认档与默认配置串都必须在表内（否则启动即拿内置档参数去比别的量纲）。"""
    assert calibration.has_tier(calibration.DEFAULT_TIER)
    assert calibration.has_tier(config.DEFAULT_CONFIG["embedding"]["model"])


def test_documented_tier_table_matches_code():
    """`docs/embedding.md` 两级默认表里"配置值"列的档，必须**要么已标定、要么显式登记为缺口**。

    只取表格第二列（配置值列），且该列须整体是一个反引号串——说明文字里作为
    **反面教材**提到的裸名（`onnx:bge-small-zh-v1.5`）不参与这条比对。"""
    doc = EMBEDDING_DOC.read_text(encoding="utf-8")
    section = doc.split("## 两级默认", 1)[1].split("\n## ", 1)[0]
    tiers = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 4:
            continue
        matched = re.fullmatch(r"`([^`]+)`", cells[2])
        if matched:
            tiers.append(matched.group(1))
    assert tiers, "文档里应能解析出档位配置值列"
    for tier in tiers:
        assert calibration.has_tier(tier) or tier in calibration.UNCALIBRATED_TIERS, (
            f"文档写了档位 {tier!r}：既不在标定表里，也没登记为已声明缺口"
        )


def test_english_tier_gap_is_actually_closed():
    """八轮 V6：原先登记的英文档缺口必须真的量出来并进了表（不是把登记删掉了事）。"""
    tier = "onnx:Xenova/bge-small-en-v1.5"
    assert calibration.has_tier(tier), "英文档没进 TIER_PARAMS，等于缺口没消解"
    assert tier not in calibration.UNCALIBRATED_TIERS
    params = calibration.TIER_PARAMS[tier]
    # 0.649 = 2026-09-19 `scripts/calibrate.py --lang en` 实测中点
    # （正样本 0.728–0.8371／负样本 0.4452–0.5705，两分布可分）
    assert params["answer_floor"] == 0.649
    assert params["absolute_floor"] < params["answer_floor"], "召回地板必须低于证据线"


def test_uncalibrated_registry_still_warns_on_apply(tmp_path, capsys, monkeypatch):
    """未标定档**必须出声**（前身缺陷是静默换量纲）。

    表清空后这条不能再靠"集合里有内容"成立（那就是空转假绿），
    所以临时登记一个假档，验证登记→告警这条机制本身还在。
    """
    from hippocampus.memory import database as db

    fake = "onnx:Xenova/newly-documented-tier"
    monkeypatch.setattr(calibration, "UNCALIBRATED_TIERS", frozenset({fake}))
    conn = db.connect(tmp_path / "warn-uncalibrated.db")
    try:
        params = calibration.apply_tier_params(conn, fake, force=True)
    finally:
        conn.close()
    assert params.get("embedding_tier") == fake
    err = capsys.readouterr().err
    assert "无标定值" in err and "未标定缺口" in err, "登记为缺口的档没有走缺口的告警文案"


def test_unknown_tier_warns_and_falls_back(tmp_path, capsys):
    """完全陌生的档（既没标定也没登记）：告警＋参数回落内置档，但不静默。"""
    from hippocampus.memory import database as db

    conn = db.connect(tmp_path / "unknown.db")
    try:
        params = calibration.apply_tier_params(conn, "onnx:Someone/not-calibrated", force=True)
    finally:
        conn.close()
    err = capsys.readouterr().err
    assert "换嵌入档没换量纲" in err
    for key, value in calibration.TIER_PARAMS[calibration.DEFAULT_TIER].items():
        assert params.get(key) == value


def test_documented_commands_use_loadable_names():
    """文档里可复制执行的命令行不得出现裸名档（那会换型失败并静默回退）。"""
    doc = EMBEDDING_DOC.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in doc.splitlines()
        if re.search(r"(HIPPOCAMPUS_EMBEDDING_MODEL=|--model\s+)onnx:(?![A-Za-z0-9._-]+/)", line)
    ]
    assert not offenders, f"命令行里的裸名 onnx 档会静默回退 MiniLM：{offenders}"


def test_query_instruction_table_aligns_with_tier_table():
    """查询指令表与档位表互指：登记了检索指令的档，档位表必须有同名键。"""
    for repo in embedding_models._QUERY_INSTRUCTION_BY_MODEL:  # noqa: SLF001
        assert calibration.has_tier(f"onnx:{repo}"), f"{repo} 配了查询指令却不在档位表里"


def test_apply_tier_params_actually_lands_for_zh_tier(tmp_path):
    """核心不变式（七轮 T1 验收①）：按文档口径的配置串换档，参数必须**真的落进快照**。"""
    from hippocampus.memory import database as db

    conn = db.connect(tmp_path / "memory.db")
    try:
        params = calibration.apply_tier_params(conn, ZH_TIER_KEY)
        assert params.get("embedding_tier") == ZH_TIER_KEY
        for key, value in calibration.TIER_PARAMS[ZH_TIER_KEY].items():
            assert params.get(key) == value, f"{key} 未按档位覆盖（换量纲失效）"
        stored = json.loads(conn.execute("SELECT params FROM param_snapshots WHERE is_active=1").fetchone()[0])
        assert stored["embedding_tier"] == ZH_TIER_KEY
    finally:
        conn.close()


def test_zh_answer_floor_below_measured_positive_min():
    """标定溯源闸：中文档证据线必须低于 2026-09-19 实测正样本最小值 0.5255，
    否则会把真命中判成"无依据"（前身报告值 0.55 就犯了这个错）。"""
    measured_positive_min = 0.5255  # scripts/calibrate.py --model onnx:Xenova/bge-small-zh-v1.5
    assert calibration.TIER_PARAMS[ZH_TIER_KEY]["answer_floor"] <= measured_positive_min
