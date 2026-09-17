"""N18 发布物收口（E4/F2/F3/F6）：跨会话专名断言、前身 BLOCKED 两条的结论锚点、CI 覆盖不静默跳过。

验收口径（缺口清单 N18）：
①（E4）0.2.0 GitHub 直装复跑 —— 见 `docs/install-verify.md` 的复跑命令（本文件不联网）；
②（F1）框架弹药卡落盘 —— `docs/framework-ammo.md`（本文件断言它存在且未被误删）；
③（F2）**名字里带"跨会话"的专名测试**：换会话 + 重开进程后仍能注入；
④（F3）前身 BLOCKED 两条逐条结论的**可复跑证据**：
   · 「验收脚本依赖代理运行」→ 本仓库所有真服务测试**自建服务**（不依赖预先在跑的代理进程）；
   · 「b2 第二重检索兜底断言失败」→ 第二重守卫（`retrieval_guard`）有测试且绿；
⑤（F6）CI 里真服务／流式／鉴权路径**不允许静默跳过**（先断言 proxy extra 在，再跑这些文件）。
"""

from __future__ import annotations

from pathlib import Path

from hippocampus.core import MemoryCore, Scope

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------ ③ 跨会话专名测试（F2）


def test_跨会话记忆可注入(home):
    """**跨会话**专名测试：会话 A 写入 → 关掉进程（重开 core）→ 会话 B 仍能检索到。

    这条钉的是"记忆属于账户，不属于某次会话"（A7）；开新会话、换 session 名、重启进程
    都不该让记忆消失（同一 account 下）。
    """
    scope_a = Scope(account="xsec", session="talk-1", source="user")
    core = MemoryCore(home=home)
    try:
        core.write(scope_a, "我的期望城市是杭州", kind="preference", source_quote="用户原话")
    finally:
        core.close()  # 模拟"这个进程结束了"

    # 新进程（新 core）+ 新会话：同一账户的另一段对话
    core2 = MemoryCore(home=home)
    try:
        scope_b = Scope(account="xsec", session="talk-2", source="user")
        injection = core2.inject_finalize(scope_b, "我的期望城市是哪里")
        assert any("杭州" in item.content for item in injection.items), "换会话+重启后仍应注入到"
    finally:
        core2.close()


def test_跨会话确认块可裁决(home):
    """跨会话的确认块也能裁决：挂起在会话 A，会话 B（新进程）里 `确认 n` 依旧生效（A35）。"""
    core = MemoryCore(home=home)
    try:
        scope_a = Scope(account="xsec2", session="talk-1", source="user")
        core.write(scope_a, "我的期望城市是北京", kind="preference", source_quote="用户原话")
        core.write(scope_a, "我的期望城市是杭州", kind="preference", source_quote="用户改口")
        assert core.pending(scope_a), "应挂起待确认"
    finally:
        core.close()

    core2 = MemoryCore(home=home)
    try:
        scope_b = Scope(account="xsec2", session="talk-2", source="user")
        pending = core2.pending(scope_b)
        assert pending, "新会话应能看到未决确认块（落库，不只在内存）"
        candidate = next(p for p in pending if p["is_new"])
        result = core2.confirm(scope_b, f"确认{candidate['num']}")
        assert result is not None and result.decision == "confirm"
        rows = {m.content: m.status for m in core2.list_memories(scope_b, limit=5, status=None)}
        assert rows.get("我的期望城市是杭州") == "active"
    finally:
        core2.close()


# ------------------------------------------------ ④ 前身 BLOCKED 两条结论（F3）


def test_验收不依赖预先运行的代理进程(home):
    """「验收脚本依赖代理运行」= 已解：真服务测试**自己起服务**（uvicorn 线程 + 空闲端口）。

    判据（可复跑）：凡是连 HTTP 的测试文件，都必须自己构造服务；CI 里不预先起任何代理进程
    （见 `.github/workflows/ci.yml`：没有任何 `hippocampus proxy` 常驻步骤）。
    """
    # ① 真 HTTP 的：自建服务 + 空闲端口（不连外部进程）
    real_server_tests = [
        ROOT / "tests" / "test_t6_real_server.py",
        ROOT / "tests" / "test_proxy_formats.py",
        ROOT / "tests" / "test_n15_proxy_tools.py",
    ]
    for path in real_server_tests:
        src = path.read_text(encoding="utf-8")
        assert "uvicorn" in src, f"{path.name} 应自建服务（uvicorn）"
        assert 'sock.bind(("127.0.0.1", 0))' in src, f"{path.name} 应取空闲端口自建服务，而不是连预先在跑的代理"
    # ③ 用 TestClient 直连进程内 app 的（无端口、无外部进程）
    auth_src = (ROOT / "tests" / "test_n7_auth_passthrough.py").read_text(encoding="utf-8")
    assert "TestClient" in auth_src, "鉴权测试应在进程内跑（不依赖外部代理进程）"
    # ② 进程内跑 ASGI 的（不需要端口）：用 TestClient 直连自己构造的 app
    inproc_src = (ROOT / "tests" / "test_n6_streaming.py").read_text(encoding="utf-8")
    assert "TestClient" in inproc_src and "build_app" in inproc_src, "流式测试应在进程内构造 app（不依赖外部进程）"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "hippocampus proxy" not in ci, "CI 不应依赖常驻代理进程"


def test_第二重检索兜底守卫有测试且可跑(home):
    """「b2 第二重检索兜底断言失败」= 已解：守卫有接线测试（且离线档也有规则兜底）。

    可复跑：`pytest tests/test_n1_guards_wired.py tests/ported/test_retrieval_guard.py -q`
    （本测试只断言"锚点在"，避免把两个文件的内容复制进来）。
    """
    assert (ROOT / "tests" / "test_n1_guards_wired.py").exists(), "N1 守卫接线测试在"
    assert (ROOT / "tests" / "ported" / "test_retrieval_guard.py").exists(), "第二重守卫单测在"
    from hippocampus.memory import retrieval_guard

    assert callable(retrieval_guard.run_retrieval_guard)
    assert "第二重" in (retrieval_guard.run_retrieval_guard.__doc__ or ""), "守卫文档应写明是第二重"


# ------------------------------------------------ ⑤ CI 覆盖不静默跳过（F6）


def test_ci_covers_form_paths_without_silent_skip():
    """CI 的 full 任务必须：先断言 proxy extra 在（否则真服务测试会 skip），再跑形态测试文件。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "import uvicorn, fastapi, httpx" in ci, "CI 应先断言 proxy extra 已装（防静默 skip）"
    for name in ("test_t6_real_server.py", "test_n6_streaming.py", "test_n7_auth_passthrough.py"):
        assert name in ci, f"CI 应显式跑 {name}"
    assert "阈值断言" in ci, "CI 的 demo 步骤应有分数阈值断言（N17-C2）"


# ------------------------------------------------ ② 发布物存在性（F1）


def test_release_docs_present():
    """弹药卡与基准协议都在仓里（F1／eval 口径），不是只在本地消息里。"""
    for rel in ("docs/framework-ammo.md", "docs/benchmark.md", "docs/forms-parity.md", "docs/verification-design.md"):
        path = ROOT / rel
        assert path.exists(), f"{rel} 应落盘"
        assert path.stat().st_size > 500, f"{rel} 不应是空壳"


def test_install_command_documented():
    """GitHub 直装的复跑命令写在文档里（E4 的口径：命令可复制、含 extras）。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "git+https://github.com/Chestnuts-Sisyphus/hippocampus" in readme
    assert "[vector,proxy]" in readme
