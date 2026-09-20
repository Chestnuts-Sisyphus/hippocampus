"""十轮 X16：scripts/ ↔ CI 接线的**对账闸**（14 个缺失脚本逐条销账）。

缺陷原型：`scripts/` 里一半脚本从未在 CI 跑过，其中不少文档还声称"可复跑/CI 可带"
（bench_multi_account、demo_flow 就是本轮才接线的）。没有这个闸，下一次漂移照样发生。

判据（双向）：
1. scripts/ 每个 .py 必须 ①被 `.github/workflows/ci.yml` 引用，或 ②在下方
   `CI_EXEMPT` 台账登记理由——**新增脚本不接线就必须写理由**，否则红；
2. ci.yml 引用的每个 scripts/*.py 必须真实存在（防引用被删脚本的假绿）。

跑法：`pytest tests/test_n53_ci_scripts_parity.py`（纯静态，零出站）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"

# 十轮 X16 销账台账：本轮不接 CI 的脚本，逐条给理由（改判=接线后从此表删除）。
CI_EXEMPT: dict[str, str] = {
    # —— 依赖仓外付费评测产物（批次冻结，只读审计件；正跑口径见 docs/benchmark.md）——
    "bench_judge_audit.py": "读仓外官方判分 JSON（付费产物不入仓）；复跑口径 benchmark.md §二·三",
    "bench_judge_cross.py": "同上：跨批复判可行性检查，输入是仓外两份 500 题报告",
    "bench_judge_subset.py": "十轮 X14 派生切片器，输入是仓外 lme_official_500b.json；命令写进 benchmark.en.md 注释",
    "bench_error_attribution.py": "官方分错误归因（T11），输入为仓外付费批次报告",
    # —— 重批次（全量评测/下载大模型，非 CI 时间预算档）——
    "bench_ab.py": "A/B 全量对照批次，需仓外数据集 JSON＋神经档下载，本机复跑（benchmark.md §二）",
    "bench_ablation.py": "批 B 全量复测（k=8/k=20），同上属重批次",
    "bench_diag.py": "失败归因需既有 A/B 数据根（--home 复用导入），本机复跑（benchmark.md §二·二b）",
    "bench_head_to_head.py": "同批 home 对照（需既有导入根＋数据集），本机复跑件",
    "bench_scale.py": "规模档延迟台账件（roadmap §七），跑一遍超 CI 预算；小样本哨兵已由 ablation_channels 承担",
    "calibrate.py": "标定重档（神经档要下载＋sha256 校验）；CI 只保标定表的静态闸 test_n48，量参数属换档事件非常态",
    # —— 显式声明"不进 CI"的一次性真机件 ——
    "live_l3_probe.py": "脚本 docstring 自声明'不进 CI'：外站探测需栗子在场人工批（默认关）",
    # —— 前身迁移工具（历史件，功能已完成，留着仅为溯源）——
    "vendor_memory_modules.py": "一次性 vendoring 工具（T2 已完成），无回归价值",
    "vendor_proxy_modules.py": "同上",
    "vendor_tests.py": "同上（随迁测试已在 tests/ported/）",
}


def _ci_referenced_scripts() -> set[str]:
    text = CI_YML.read_text(encoding="utf-8")
    return set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", text))


def test_every_script_is_wired_or_exempt():
    referenced = _ci_referenced_scripts()
    on_disk = {p.name for p in (ROOT / "scripts").glob("*.py")}
    unwired = on_disk - referenced
    unregistered = unwired - set(CI_EXEMPT)
    assert not unregistered, (
        f"scripts/ 下这些脚本既未接 CI 也未登记豁免理由：{sorted(unregistered)}（接线，或进 CI_EXEMPT 写理由）"
    )


def test_exempt_entries_are_still_needed():
    referenced = _ci_referenced_scripts()
    on_disk = {p.name for p in (ROOT / "scripts").glob("*.py")}
    stale_exempt = sorted(k for k in CI_EXEMPT if k in referenced or k not in on_disk)
    assert not stale_exempt, f"豁免台账陈旧条目（已接线或文件不存在，应从台账销账）：{stale_exempt}"


def test_ci_references_exist_on_disk():
    referenced = _ci_referenced_scripts()
    missing = sorted(s for s in referenced if not (ROOT / "scripts" / s).exists())
    assert not missing, f"ci.yml 引用了不存在的脚本（假绿）：{missing}"
