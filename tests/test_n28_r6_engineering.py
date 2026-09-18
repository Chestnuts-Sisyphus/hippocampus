"""六轮工程小修（C1/C2/C6/C7 批量）：ingest 精确去重、_locks 逐出、psutil 声明、CLI help 漂移。

验收（任务书 G6）：
- ingest_history 精确去重：内容规范化相等即跳过；两次同批导入只入一次（≥2 测试）；
- LRU 逐出连带清该账户 `_locks`（≥1 测试），本地持有的锁保留；
- pyproject dev extras 声明 psutil（脚本内已留缺装降级）；
- CLI 帮助行不再宣称 `--candidates` flag（parser 从没有过它）。
"""

from __future__ import annotations

import threading

import pytest

from hippocampus.core import MemoryCore, Scope


@pytest.fixture()
def scope():
    return Scope(account="r6", session="s1", source="user")


# ------------------------------------------------ C1 ingest 精确去重


def test_ingest_dedup_same_batch_twice(core, scope):
    """两次同批导入只入一次：第二遍两池全跳，行数不变，skipped 如实计数。"""
    turns = [
        {"text": "我们讨论过排期：下周发版", "role": "user"},
        {"text": "好的，我记下了", "role": "assistant", "session_id": "s1"},
    ]
    out1 = core.ingest_history(scope, turns)
    before = core.stats(scope)
    out2 = core.ingest_history(scope, turns)
    after = core.stats(scope)
    assert out1["skipped"] == 0 and out1["memories"] == 2
    assert out2 == {"episodes": 0, "memories": 0, "skipped": 2}
    assert after["memories"] == before["memories"] == 2
    assert after["episodes"] == before["episodes"] == 2


def test_ingest_dedup_normalized_equal(core, scope):
    """规范化相等（全角/标点/空白差异）也判重复跳过。"""
    turns = [{"text": "  我住在天津！ ", "role": "user", "session_id": "s9"}]
    core.ingest_history(scope, turns)
    turns2 = [{"text": "我住在天津。", "role": "user", "session_id": "s9"}]
    out = core.ingest_history(scope, turns2)
    assert out["skipped"] == 1 and core.stats(scope)["memories"] == 1


def test_ingest_dedup_keeps_distinct(core, scope):
    """内容不同的轮照常入库（去重不过头）。"""
    turns = [
        {"text": "第一次说的内容", "role": "user", "session_id": "s1"},
        {"text": "第二次说的内容", "role": "user", "session_id": "s1"},
        {"text": "第三次说的内容", "role": "assistant", "session_id": "s1"},
    ]
    out = core.ingest_history(scope, turns)
    assert out == {"episodes": 3, "memories": 3, "skipped": 0}


def test_ingest_dedup_pool_switch_keeps_missing_pool(core, scope):
    """as_memory/as_episode 开关跨批变化：没重复的那个池照常落。"""
    turn = {"text": "只进记忆不进经历的轮", "role": "user", "session_id": "s3"}
    out1 = core.ingest_history(scope, [turn], as_memories=True)
    assert out1["episodes"] == 1 and out1["memories"] == 1
    # 第二遍：同一轮要求 as_episode=False、as_memory=True —— 记忆已去重、经历本就不要求
    out2 = core.ingest_history(scope, [{**turn, "as_episode": False}], as_memories=True)
    assert out2 == {"episodes": 0, "memories": 0, "skipped": 0}
    # 第二遍：要求 as_episode=True、as_memory=False —— 两池都不落（经历判重跳过，记忆不要求）
    out3 = core.ingest_history(scope, [{**turn, "as_memory": False}], as_memories=False)
    assert out3 == {"episodes": 0, "memories": 0, "skipped": 0}
    assert core.stats(scope)["episodes"] == 1


def test_ingest_dedup_cross_session_same_account(core, scope):
    """同账户跨会话同内容：记忆按规范化全局判重（T10 场景——同 home 二跑不滚大库）；
    新会话的经历轮仍落（会话不同不判重），记忆不重复。"""
    turns = [{"text": "另一段会话的同一句", "role": "user", "session_id": "sA"}]
    core.ingest_history(scope, turns)
    turns2 = [{"text": "另一段会话的同一句", "role": "user", "session_id": "sB"}]
    out = core.ingest_history(scope, turns2)
    assert out["memories"] == 0 and out["episodes"] == 1
    assert core.stats(scope)["memories"] == 1


# ------------------------------------------------ C2 _locks 逐出


def test_lru_eviction_clears_locks(home, monkeypatch):
    """LRU 逐出连带清 _locks：上限 2 开 3 账户 → 最旧账户的锁对象也删了。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    try:
        for i in range(3):
            s = Scope(account=f"lk-{i}", session="s1", source="user")
            core.write(s, f"账户 lk-{i} 的记忆", kind="fact")
        with core._registry_guard:  # noqa: SLF001
            assert "lk-0" not in core._locks, "被逐出账户的 WriterLock 应一并清除"
            assert set(core._locks.keys()) == {"lk-1", "lk-2"}
    finally:
        core.close()


def test_locks_kept_when_held_during_eviction(home, monkeypatch):
    """逐出时若该账户的 WriterLock 正被本地持有 → 锁对象保留（等用完后由后续逐出清）。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    entered = threading.Event()
    release = threading.Event()
    try:
        holder_scope = Scope(account="held-acc", session="s1", source="user")
        core.write(holder_scope, "被持有的账户", kind="fact")
        lock = core._lock("held-acc")  # noqa: SLF001

        def holder():
            with lock.held():
                entered.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        assert entered.wait(3), "持有线程未进入锁内"
        # 开更多账户触发逐出（上限 2；held-acc 是"最近用"，先被逐出的会是别人）
        for i in range(2):
            s = Scope(account=f"other-{i}", session="s1", source="user")
            core.write(s, f"other {i}", kind="fact")
        # 强制让 held-acc 成为最久未用后再触发一次逐出
        core._session(Scope(account="touch", session="s1", source="user"))  # noqa: SLF001
        core.write(Scope(account="touch2", session="s1", source="user"), "touch2", kind="fact")
        with core._registry_guard:  # noqa: SLF001
            assert "held-acc" in core._locks, "本地仍持有 → 锁对象不得删"
    finally:
        release.set()
        t.join(timeout=5)
        core.close()


# ------------------------------------------------ C6/C7 声明与帮助行


def test_pyproject_declares_psutil():
    """psutil 进 dev extras（六轮 C6）：fresh 环境跑 bench_multi_account 不再 ModuleNotFoundError。"""
    from pathlib import Path

    text = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "psutil>=5.9" in text


def test_cli_help_line_has_no_candidates_flag():
    """CLI 帮助行不再宣称 --candidates（parser 只有 --pending/--suspicious，C7 六轮）。"""
    from pathlib import Path

    text = Path("src/hippocampus/cli.py").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.strip().startswith("memory"))
    assert "--candidates" not in line
    assert "--pending/--suspicious" in line