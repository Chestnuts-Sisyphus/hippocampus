"""回归：确认（挂起裁决）之后注入集合随之更新。

这条用例来自一次真实排查：把"候选转正"这一步漏掉时，胜出的记忆仍是 `candidate`
状态、进不了注入——表面看"确认成功"，实际后续行为没变。所以这里把
「挂起 → 裁决 → 注入集合更新」整条链钉成回归。

（文件名沿用了排查时的临时名，内容已是正式用例。）
"""

from __future__ import annotations


def test_confirm_updates_injection_set(core, scope):
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")

    pending = core.pending(scope)
    assert pending, "同对象取值不同应挂起"
    candidate = next(p for p in pending if p["is_new"])
    core.confirm(scope, f"确认{candidate['num']}")

    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows["我的期望城市是杭州"] == "active", "候选确认后必须转正（否则进不了注入）"
    assert rows["我的期望城市是北京"] == "superseded"

    search = core.search(scope, "我的期望城市是哪里", limit=10)
    assert [i.content for i in search.items] == ["我的期望城市是杭州"]

    injection = core.inject_finalize(scope, "我的期望城市是哪里")
    assert [i.content for i in injection.items] == ["我的期望城市是杭州"]
    # 注入文本里的"上次相关对话"段落会引用经历原文（那是经历层，不是记忆层），
    # 所以这里只断言**记忆条目**里没有旧值
    assert "[preference] 我的期望城市是北京" not in injection.fluid_text


def test_pending_survives_process_restart(home, scope):
    """挂起的确认必须**跨进程**存在。

    排查时发现的真缺陷：前身把待确认队列放在内存里，进程一退就没了——库里那条记忆
    还挂着 candidate（既不生效也不消失），用户下个会话再也无法裁决。
    """
    from hippocampus.core import MemoryCore

    first = MemoryCore(home=home)
    try:
        first.write(scope, "我的期望城市是北京", kind="preference")
        first.write(scope, "我的期望城市是杭州", kind="preference")
        before = first.pending(scope)
        assert before, "同进程内应有挂起"
    finally:
        first.close()

    second = MemoryCore(home=home)  # 模拟"下个会话 / 另一个进程"
    try:
        restored = second.pending(scope)
        assert [p["content"] for p in restored] == [p["content"] for p in before], "重启后应能恢复挂起队列"
        candidate = next(p for p in restored if p["is_new"])
        outcome = second.confirm(scope, f"确认{candidate['num']}")
        assert outcome is not None and outcome.decision == "confirm"
        rows = {m.content: m.status for m in second.list_memories(scope, limit=5, status=None)}
        assert rows["我的期望城市是杭州"] == "active"
        assert second.pending(scope) == [], "裁决后队列应清空"
    finally:
        second.close()
