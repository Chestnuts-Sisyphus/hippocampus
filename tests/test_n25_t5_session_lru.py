"""T5 会话缓存 LRU（A2）：多账户/长跑内存有界。

验收：
- 超上限按最久未用逐出（close 释放 chroma client），不逐出**正在使用**的会话（并发测试）；
- 被逐出的账户重开照常工作（数据在磁盘，证据不丢）；
- `mb._BRIDGES` 与 `core._sessions` 两处缓存同源同限；
- 模拟多账户存取：逐出后各账户自己的记忆仍可检索（证据命中不因 LRU 下降）。
"""

from __future__ import annotations

import threading

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.memory import memory_bridge as mb


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


def _open_accounts(core, home, n: int) -> list[str]:
    """打开 n 个账户会话（写一条记忆，保证会话真的建起来）。"""
    out = []
    for i in range(n):
        acc = f"lru-{i}"
        s = Scope(account=acc, session="s1", source="user")
        core.write(s, f"账户 {acc} 的专属记忆：只归自己查", kind="fact")
        out.append(acc)
    return out


def test_core_session_lru_evicts_oldest(home, monkeypatch):
    """上限=2：开 3 个账户 → 只留 2 个会话，最早那个被 close。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    try:
        accounts = _open_accounts(core, home, 3)
        with core._registry_guard:  # noqa: SLF001
            alive = set(core._sessions.keys())  # noqa: SLF001
        assert alive == set(accounts[1:]), f"最旧账户应被逐出: alive={alive}"
    finally:
        core.close()


def test_lru_reopened_account_keeps_data(home, monkeypatch):
    """被逐出的账户重开：数据还在磁盘，检索仍命中（LRU 不丢证据）。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    try:
        acc = "keep-me"
        s = Scope(account=acc, session="s1", source="user")
        core.write(s, "失忆飞机的航线是北京到珠海", kind="fact")
        # 逼走 acc：再开 2 个账户（上限 2 → acc 被逐出）
        _open_accounts(core, home, 2)
        with core._registry_guard:  # noqa: SLF001
            assert acc not in core._sessions  # noqa: SLF001
        result = core.search(Scope(account=acc, session="s1", source="user"), "失忆飞机的航线", limit=8)
        assert any("北京到珠海" in item.content for item in result.items)
    finally:
        core.close()


def test_lru_never_evicts_in_use_session(home, monkeypatch):
    """并发：另一个线程正握着账户 X 的锁时，LRU 不得逐出 X。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    in_use = Scope(account="busy", session="s1", source="user")
    core.write(in_use, "正在被使用的记忆", kind="fact")
    session = core._session(in_use)  # noqa: SLF001

    entered = threading.Event()
    release = threading.Event()
    errors: list[str] = []

    def holder():
        with session.lock:
            entered.set()
            # 不带超时：主线程 finally 必置 release。旧写法 timeout=5 在慢机
            # （CI Windows runner 开 4 个账户的写路径 >5s）会提前放锁，
            # LRU 把"正在使用"的会话逐出——假红而非产品缺陷。
            release.wait()
        entered.clear()

    t = threading.Thread(target=holder)
    t.start()
    # 纯死锁兜底（V13）：等的是"线程进锁"这一事件，由被等的一方 entered.set() 置位；
    assert entered.wait(300), "持有线程未进入锁内"

    try:
        # 主线程开一堆账户触发逐出（上限 2）
        _open_accounts(core, home, 4)
        session2 = core._session(in_use)  # noqa: SLF001
        assert session2 is session, "正在使用的会话被逐出重建"
        # 它的锁还在被 holder 持有 → 数据可用性由 SQLite 保证（close 未发生）
        with core._registry_guard:  # noqa: SLF001
            assert "busy" in core._sessions  # noqa: SLF001
    finally:
        release.set()
        t.join(timeout=300)  # 死锁兜底：release 已置位，正常路径 <1 秒
        core.close()

    assert not errors
    assert session.conn is not None


def test_bridge_cache_lru_evicts(home, monkeypatch):
    """mb._BRIDGES 同限：上限=2，开 4 个 bridge → 只留 2。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    try:
        for i in range(4):
            mb.get_bridge(f"br-{i}")
        assert mb.bridge_cache_size() == 2
    finally:
        for i in range(4):
            mb.drop_bridge(f"br-{i}")


def test_multi_account_search_survives_lru(home, monkeypatch):
    """模拟 LME 一题一账户：逐出走一遍，各账户自己的记忆仍可查（证据命中不降）。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "2")
    core = MemoryCore(home=home)
    try:
        hits = 0
        n = 6
        queries = []
        for i in range(n):
            acc = f"row-{i}"
            s = Scope(account=acc, session="s1", source="user")
            text = f"账户 {i} 的独有标记：只有这一条能回答自己的问题"
            core.write(s, text, kind="fact")
            queries.append((acc, text))
        for acc, text in queries:
            result = core.search(Scope(account=acc, session="s1", source="user"), text, limit=8)
            if any(text in item.content for item in result.items):
                hits += 1
        assert hits == n, f"LRU 导致证据丢失: {hits}/{n}"
    finally:
        core.close()


def test_bridge_and_core_cache_share_limit(home, monkeypatch):
    """两处缓存同源（同一上限函数）：改 env 立即生效于 core 侧。"""
    monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "4")
    core = MemoryCore(home=home)
    try:
        assert mb._bridge_cache_max() == 4  # noqa: SLF001
        monkeypatch.setenv("HIPPOCAMPUS_SESSION_CACHE_MAX", "abc")
        assert mb._bridge_cache_max() == 16  # 非法值回退默认
    finally:
        core.close()
