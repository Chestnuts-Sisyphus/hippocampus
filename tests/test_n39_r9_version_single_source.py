"""九轮 W2：版本单源＋"六处一致"闸（缺口 K2／K3／K9）。

八轮 V7 发版时暴露的真实漂移（本轮开工复核仍在）：
- `src/hippocampus/__init__.py` 的 `__version__` 是手写的，停在 **0.2.1**，而 `pyproject.toml` 已是 **0.4.0**
  → `hippocampus --version` 打印的是旧版本（K2）；
- `build_app()` 的 `FastAPI(version=...)` 另写死一个 **0.1.0**（K3）；
- 发版纪律两处文本一处写"四处一致"（CHANGELOG 顶部段）一处写"五处"（`docs/release-sync.md`）（K9）。

现在版本只有一个来源（安装元数据 ← `pyproject.toml`），其余各处由机器闸核对。
六个取值点：`pyproject.toml` ／ `hippocampus.__version__` ／ `importlib.metadata`
／ FastAPI `app.version` ／ `uv.lock` 根包 ／ `CHANGELOG.md` 顶部版本段。

含**"改一处必红"对照测试**：六个源任意一个单独改成别的值，闸都必须只报出那一处。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\[project\][\s\S]*?^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 的 [project] 段里没有 version"
    return m.group(1)


def _uvlock_root_version() -> str:
    text = (REPO / "uv.lock").read_text(encoding="utf-8")
    m = re.search(r'name = "hippocampus-agent"\nversion = "([^"]+)"', text)
    assert m, "uv.lock 里找不到根包 hippocampus-agent 的 version"
    return m.group(1)


def _changelog_top_version() -> str:
    """CHANGELOG 顶部**第一个真实版本段**（`[Unreleased]` 不算版本）。"""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    assert m, "CHANGELOG 顶部找不到 ## [x.y.z] 版本段"
    return m.group(1)


def _app_version(core) -> str:
    from hippocampus.proxy.app import build_app

    return str(build_app(core, offline=True, upstream=None, auth_token="").version)


def version_sources(core) -> dict[str, str]:
    """六个取值点（键名即口径名，报错时一眼看出哪一处漂了）。"""
    import hippocampus

    return {
        "pyproject.toml": _pyproject_version(),
        "hippocampus.__version__": hippocampus.__version__,
        "importlib.metadata": metadata.version("hippocampus-agent"),
        "FastAPI app.version": _app_version(core),
        "uv.lock 根包": _uvlock_root_version(),
        "CHANGELOG 顶部段": _changelog_top_version(),
    }


def mismatches(sources: dict[str, str]) -> list[str]:
    """返回与多数值不一致的源名；全一致时为空列表。"""
    counts: dict[str, int] = {}
    for value in sources.values():
        counts[value] = counts.get(value, 0) + 1
    canonical = max(counts.items(), key=lambda kv: kv[1])[0]
    return [name for name, value in sources.items() if value != canonical]


def test_six_version_sources_agree(core):
    sources = version_sources(core)
    bad = mismatches(sources)
    assert not bad, f"版本不一致：{ {k: sources[k] for k in bad} }"


def test_gate_turns_red_when_any_single_source_drifts(core):
    """对照测试：六个源逐个植入漂移 → 每次都只报出被改的那一处（闸不是空转）。"""
    for name in sorted(version_sources(core)):
        sources = dict(version_sources(core))
        sources[name] = "9.9.9"
        assert mismatches(sources) == [name], f"改 {name} 未报红"


def test_cli_version_matches_pyproject(capsys):
    """验收口径：`hippocampus --version` 与 pyproject 同值（曾长期显示 0.2.1）。"""
    from hippocampus import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"hippocampus {_pyproject_version()}"


def test_subprocess_entrypoint_reports_installed_version():
    """真实入口再起一个进程核对：`__version__` ← 安装元数据 ← pyproject 这条链不靠 in-process 巧合。"""
    code = (
        "from hippocampus import __version__;"
        "from importlib import metadata;"
        "print(__version__, metadata.version('hippocampus-agent'))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "HIPPOCAMPUS_OFFLINE": "1"},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    printed, meta = proc.stdout.split()
    assert printed == meta == _pyproject_version()


def test_discipline_text_says_six_places():
    """K9：纪律文本归一——两处都**规定**"六处"（旧口径的"四处／五处"只许出现在归一说明里）。"""
    ch = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    rs = (REPO / "docs/release-sync.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[\d+\.\d+\.\d+\][\s\S]{0,900}", ch, re.M)
    assert m and "版本六处一致" in m.group(0), "CHANGELOG 顶部版本段未规定六处一致"
    assert '版本号一致是"六处"' in rs, "release-sync 未把一致面写成六处"
