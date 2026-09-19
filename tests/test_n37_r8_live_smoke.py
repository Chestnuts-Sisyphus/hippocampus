"""八轮 V5＋V9：真机冒烟进 pytest 常驻，并把 `/trace` 的**真响应字段**与文档钉在一起。

和 `scripts/live_management_smoke.py` 共用同一份实现（不写第二套起服务代码）：
本文件把它当成 fixture 用，一次真进程冒烟的结果既喂语义断言，也喂文档一致性断言。

V13 纪律在这里也成立：起服务用的是**轮询就绪**（`/health` 回 200 就往下走），
`STARTUP_DEADLINE_S` 只是纯死锁兜底，不是"预计 8 秒所以等 8 秒"那种功能性预算。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO / "scripts" / "live_management_smoke.py"
DEPLOYMENT = REPO / "docs" / "deployment.md"

pytest.importorskip("uvicorn", reason="真机冒烟需要 uvicorn（proxy extra）")


def _smoke_module():
    spec = importlib.util.spec_from_file_location("live_management_smoke", SMOKE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


smoke = _smoke_module()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """真起两次管理口（干净一台＋端点必坏一台），返回结果字典。"""
    root = tmp_path_factory.mktemp("live-smoke")
    return smoke.run_smoke(Path(root))


def test_five_http_semantics_in_real_process(live):
    """200／401／400／404／502 五类语义都必须在真进程下成立（pytest 全绿≠真机跑得通）。"""
    missing = smoke.evaluate(live)
    assert not missing, f"真机冒烟语义未成立：{missing}"


def test_process_is_cleaned_up(live):
    """收尾必杀：脚本内部 terminate→wait→kill，这里补一条"没有留下 serve 进程"的粗筛。"""
    psutil = pytest.importorskip("psutil", reason="进程清点需要 psutil（dev extra）")
    leftovers = [p for p in psutil.process_iter(["name", "cmdline"]) if _is_serve(p.info.get("cmdline"))]
    assert not leftovers, f"冒烟结束后仍有 serve 进程存活：{[p.pid for p in leftovers]}"


def _is_serve(cmdline) -> bool:
    parts = [str(x) for x in (cmdline or [])]
    return "hippocampus.cli" in " ".join(parts) and "serve" in parts


def test_trace_response_fields_match_the_docs_table(live):
    """V9 闸：`/trace` 真响应的字段必须**逐个**出现在 deployment 的返回字段表里。

    实现加了字段而文档没跟上 → 这里红；文档写了实现没有的字段 → 也红（双向）。
    """
    body = live.get("trace_body") or {}
    assert body, "真机 /trace 没回内容，文档比对不成立"
    doc = DEPLOYMENT.read_text(encoding="utf-8")
    section = _trace_fields_section(doc)
    top_level = set(body.keys())
    documented = _table_names(section["top"])
    assert top_level == documented, f"/trace 顶层字段漂移：真响应 {sorted(top_level)} vs 文档 {sorted(documented)}"

    audit = (body.get("audit") or [{}])[0]
    if audit:
        assert set(audit.keys()) == _table_names(section["audit"]), (
            f"audit 事件字段漂移：{sorted(set(audit.keys()))} vs 文档 {sorted(_table_names(section['audit']))}"
        )
        cand = (audit.get("candidates") or [{}])[0]
        if cand:
            assert set(cand.keys()) == _table_names(section["candidate"]), (
                f"candidates 字段漂移：{sorted(set(cand.keys()))} vs 文档 {sorted(_table_names(section['candidate']))}"
            )


def test_health_response_fields_match_the_docs_table(live):
    """同一条纪律管 `/health`：真响应的字段集合 == 文档所列（七轮就错过一次字段误述）。"""
    documented = _table_names(_trace_fields_section(DEPLOYMENT.read_text(encoding="utf-8"))["health"])
    assert set(live["health_fields"]) == documented, (
        f"/health 字段漂移：真响应 {sorted(live['health_fields'])} vs 文档 {sorted(documented)}"
    )


def _trace_fields_section(doc: str) -> dict[str, str]:
    marker = "#### 返回字段（八轮 V9，以真机响应与代码为准）"
    assert marker in doc, "docs/deployment.md 缺『返回字段』小节（V9 未完成）"
    chunk = doc.split(marker, 1)[1]
    parts = {
        "top": _between(chunk, "**`/trace` 顶层**", "**`audit[]`**"),
        "audit": _between(chunk, "**`audit[]`**", "**`audit[].candidates[]`**"),
        "candidate": _between(chunk, "**`audit[].candidates[]`**", "**`observe[]`**"),
        "observe": _between(chunk, "**`observe[]`**", "**`/health`**"),
        "health": _between(chunk, "**`/health`**", "\n## "),
    }
    for name, text in parts.items():
        assert text.strip(), f"返回字段小节缺 `{name}` 的表格"
    return parts


def _between(text: str, start: str, end: str) -> str:
    head = text.split(start, 1)
    if len(head) < 2:
        return ""
    return head[1].split(end, 1)[0]


def _table_names(md: str) -> set[str]:
    """取 markdown 表格第一列的反引号字段名。"""
    out: set[str] = set()
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|")[0]
        out.update(re.findall(r"`(\w+)`", first))
    out.discard("字段")
    return out
