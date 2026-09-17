"""N2 索引健康可诊断（B5）：doctor 报索引健康；chroma 不可写/同步失败时明确告警。

验收口径（缺口清单 N2）：
- `doctor` 有"索引健康"一行（chroma 目录可写性＋集合条数 vs 库内 active 条数）；
- 索引写失败/检索失败不再静默：`_index_error` 记录、`inject_finalize` 的 note 携带告警；
- 只读目录模拟：`_dir_writable` 被替换为 False 时 `index_health` 报不健康。
"""

from __future__ import annotations

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import retrieval as rt


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


@pytest.fixture()
def lexical_core(home):
    core = MemoryCore(home=home, vector=False)  # 词法档：不建 chroma 集合
    yield core
    core.close()


def test_index_health_lexical_mode(lexical_core, scope):
    """词法档：无向量集合 → collection_count None，健康结论记"不适用"。"""
    health = lexical_core.index_health(scope)
    assert health["collection_count"] is None
    assert health["healthy"] is None
    assert health["active_memories"] >= 0


def test_index_health_unwritable_dir_warns(core, scope, monkeypatch):
    """chroma 目录不可写（模拟只读/磁盘满）→ 明确报不健康，不再静默。"""
    from hippocampus.core import core as core_mod

    monkeypatch.setattr(core_mod, "_dir_writable", lambda _p: False)
    health = core.index_health(scope)
    assert health["chroma_writable"] is False
    assert health["healthy"] is False


def test_sync_failure_sets_error_and_note(core, scope, monkeypatch):
    """索引同步失败 → `_index_error` 记录；`inject_finalize` 的 note 携带告警（不静默）。"""
    session = core._session(scope)  # noqa: SLF001

    def broken_sync(*_a, **_k):
        raise OSError("模拟 chroma 写入失败：磁盘只读")

    monkeypatch.setattr(rt, "sync_index", broken_sync)
    core._reindex(session)  # noqa: SLF001
    assert session._index_error  # noqa: SLF001

    injection = core.inject_finalize(scope, "我投简历有什么要求？")
    assert "索引告警" in injection.note


def test_doctor_prints_index_health_line(core, scope, capsys):
    """`hippocampus doctor` 输出包含"索引健康"行。"""
    from hippocampus.cli import cmd_doctor

    class Args:
        home = None
        account = "test"
        session = "s1"
        unlock = False

    cmd_doctor(Args())
    out = capsys.readouterr().out
    assert "索引健康" in out
