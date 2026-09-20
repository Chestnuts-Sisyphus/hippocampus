"""八轮 V5＋V9：真机冒烟进 pytest 常驻，并把 `/trace` 的**真响应字段**与文档钉在一起。

以及 X8：增加真实端口探测逻辑。

和 `scripts/live_management_smoke.py` 共用同一份实现（不写第二套起服务代码）：
本文件把它当成 fixture 用，一次真进程冒烟的结果既喂语义断言，也喂文档一致性断言。

V13 纪律在这里也成立：起服务用的是**轮询就绪**（`/health` 回 200 就往下走），
`STARTUP_DEADLINE_S` 只是纯死锁兜底，不是"预计 8 秒所以等 8 秒"那种功能性预算。
"""

from __future__ import annotations

import importlib.util
import re
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO / "scripts" / "live_management_smoke.py"
DEPLOYMENT = REPO / "docs" / "deployment.md"

pytest.importorskip("uvicorn", reason="真机冒烟需要 uvicorn（proxy extra）")


def _port_free(host: str, port: int) -> bool:
    """X8：真实端口探测逻辑——直接尝试连接，而非依赖脚本内部判断。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result != 0
    except Exception:
        return True


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


def test_port_detection_logic():
    """X8：端口探测必须是**真实 bind 探测**（批 A 残桩订正：前版引用了脚本不存在的
    `mem_config.get_proxy_config`，且把"服务在跑"当测试前提——fixture 是 dict 并非活服务）。

    判据两条：① 脚本 `free_port()` 选出的端口必须真能 bind（自由口的定义）；
    ② 自占期间用同一探测方式必须判"占用"，释放后回到"空闲"——探测逻辑双向有效。
    """
    smoke = _smoke_module()
    port = smoke.free_port()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))  # ①：free_port 给的口要真 bind 得动
        probe.listen(1)
        assert not _port_free("127.0.0.1", port), f"自绑端口{port}后探测仍判空闲 → 探测逻辑失真"
    finally:
        probe.close()
    assert _port_free("127.0.0.1", port), f"释放后端口{port}应回到空闲（②双向判据）"


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
