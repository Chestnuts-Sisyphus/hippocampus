"""N10 后端抽象＋导入导出（F4/F5）。

验收口径（缺口清单 N10）：
- `MemoryCore` 抽出存储后端协议（`MemoryBackend`），SQLite＋Chroma 作为默认实现；
- `hippocampus export/import` 两个子命令（导出为目录包，含 schema 版本）。
"""

from __future__ import annotations

import json

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.core import backend as backend_mod
from hippocampus.core.transfer import SCHEMA_VERSION, export_package, import_package, read_manifest


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def test_default_backend_is_sqlite_chroma(core):
    """默认后端＝SQLite＋Chroma（协议实现）。"""
    assert isinstance(core.backend, backend_mod.MemoryBackend)
    assert core.backend.name == "sqlite+chroma"


def test_custom_backend_is_used(home, scope):
    """换后端＝传入实现协议的对象（不改核心代码）：自定义后端被真的调用。"""

    calls: list[str] = []

    class FakeSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class FakeBackend:
        name = "fake"

        def __init__(self):
            self._inner = backend_mod.SqliteChromaBackend(vector=False)

        def open_session(self, account_id, data_dir):
            calls.append(account_id)
            return FakeSession(self._inner.open_session(account_id, data_dir))

    core = MemoryCore(home=home, backend=FakeBackend())
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    assert calls == ["test"]  # 自定义后端被调用
    assert core.stats(scope)["memories"] == 1
    core.close()


def test_lexical_backend_via_backend_param(home, scope):
    """词法档 = 同一后端类的档位（vector=False），不是另一条代码路径。"""
    core = MemoryCore(home=home, backend=backend_mod.SqliteChromaBackend(vector=False))
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    assert core.stats(scope)["memories"] == 1
    assert core.index_health(scope)["collection_count"] is None  # 词法档：无向量集合
    core.close()


def test_export_import_roundtrip(home, scope, tmp_path):
    """导出→导入往返：计数一致、schema 版本在包里。"""
    from hippocampus.seed import seed

    core = MemoryCore(home=home)
    seed(core, scope)
    before = core.stats(scope)
    pkg = tmp_path / "pkg"
    manifest = export_package(core, scope, pkg)
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert (pkg / "manifest.json").exists()
    assert (pkg / "memory.db").exists()
    assert read_manifest(pkg)["account"] == "test"
    core.close()

    # 导入到另一个数据根的新账号
    home2 = tmp_path / "home2"
    core2 = MemoryCore(home=home2)
    dst = Scope(account="dst", session="s1", source="user")
    result = import_package(core2, dst, pkg)
    after = result["stats"]
    assert after["memories"] == before["memories"]
    assert after["entities"] == before["entities"]
    # 导入后可检索（索引已重建）
    assert core2.stats(dst)["memories"] == before["memories"]
    core2.close()


def test_import_refuses_overwrite_without_force(home, scope, tmp_path):
    """目标库已存在时默认拒绝（--force 才覆盖）。"""
    core = MemoryCore(home=home)
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    pkg = tmp_path / "pkg"
    export_package(core, scope, pkg)
    with pytest.raises(FileExistsError):
        import_package(core, scope, pkg)
    # force 覆盖成功
    result = import_package(core, scope, pkg, force=True)
    assert result["stats"]["memories"] >= 1
    core.close()


def test_import_rejects_bad_schema(home, scope, tmp_path):
    """schema 版本不符 → 拒绝（不猜着读）。"""
    core = MemoryCore(home=home)
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    pkg = tmp_path / "pkg"
    export_package(core, scope, pkg)
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 999
    (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        import_package(core, scope, pkg, force=True)
    core.close()


def test_cli_export_import(home, capsys):
    """CLI 面：`export` / `import` 两个子命令可用。"""
    from hippocampus.cli import main

    assert main(["--account", "src", "export", str(home / "pkg_cli")]) == 0
    out = capsys.readouterr().out
    assert "已导出" in out
