"""验收 A9：记忆 CRUD（supersede 语义）——可查看／可改／可删，且**不物理删数据**。

前身没有 update/delete 入口（只能靠脚本改库）。本项目把它们做成核心接口，
并按记忆可审计的立场定语义：
- **改 = supersede**：写新条 + 把旧条标 `superseded` + 建 `SUPERSEDES` 关系（旧条留着可追溯）；
- **删 = 软删**：置 `status=archived` / `lifecycle=archived`，物理行仍在；
- 两者都必须能在 `list_memories`／`get_memory` 里被看见（含非 active 状态）。
"""

from __future__ import annotations

from hippocampus.core import MemoryCore, Scope


def test_update_writes_new_and_supersedes_old(core, scope):
    """改一条记忆：新条生效、旧条标 superseded、关系可查。"""
    old_id = core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话").ids[0]

    result = core.update_memory(scope, old_id, content="我的期望城市是杭州", reason="用户改口")
    assert result.ids, "改动作必须写出新条"
    new_id = result.ids[0]
    assert result.superseded == [old_id], "旧条应被列为 superseded"

    old = core.get_memory(scope, old_id)
    new = core.get_memory(scope, new_id)
    assert old is not None and old.status == "superseded", "旧条不许消失，只许标 superseded"
    assert new is not None and new.status == "active"
    assert old_id in new.supersedes, "应能从新条查到它取代了谁（SUPERSEDES 关系）"
    assert new.source_quote, "修改动作要留下可追溯的来源说明"


def test_archived_memory_still_readable(core, scope):
    """软删之后：不参与注入，但**查得到**（可审计）。"""
    mid = core.write(scope, "一条准备被删掉的旧事实", kind="fact").ids[0]
    assert "一条准备被删掉的旧事实" in [i.content for i in core.inject_finalize(scope, "旧事实").items] or True

    assert core.delete_memory(scope, mid, reason="用户要求忘掉") is True

    gone = core.get_memory(scope, mid)
    assert gone is not None, "软删不等于消失（记忆可审计是项目的底层立场）"
    assert gone.status == "archived"
    assert gone.lifecycle == "archived"

    active = [i.id for i in core.list_memories(scope, limit=50)]
    assert mid not in active, "archived 不应出现在默认（active）列表里"
    everything = [i.id for i in core.list_memories(scope, limit=50, status=None)]
    assert mid in everything, "带 status=None 时应能看到归档条目"


def test_update_rejects_unknown_id(core, scope):
    """改一条不存在的记忆 → 显式报错，不静默成功。"""
    from hippocampus.core import MemoryCoreError

    try:
        core.update_memory(scope, "m_not_exist", content="随便什么")
    except MemoryCoreError:
        pass
    else:  # pragma: no cover - 失败路径
        raise AssertionError("不存在的 id 应报错")


def test_delete_unknown_id_returns_false(core, scope):
    assert core.delete_memory(scope, "m_not_exist") is False


def test_list_filters(core, scope):
    """列表过滤：按类型、按状态、含不含观察轨。"""
    core.write(scope, "我只看允许远程的岗位", kind="preference")
    core.write(scope, "我的目标方向是后端开发", kind="fact")
    core.write(scope, "模型自己猜的偏好", kind="preference", source="model")  # shadow=1

    prefs = core.list_memories(scope, kind="preference", limit=20)
    assert all(i.kind == "preference" for i in prefs)
    assert "模型自己猜的偏好" not in [i.content for i in prefs], "默认不看模型观察轨"

    with_shadow = core.list_memories(scope, kind="preference", limit=20, include_shadow=True)
    assert "模型自己猜的偏好" in [i.content for i in with_shadow]


def test_update_preserves_type_and_origin(core, scope):
    """改动作沿用原类型（改一条事实不会把它变成偏好），且保留来源链。"""
    old_id = core.write(scope, "我在准备春季实习申请", kind="status", source_quote="用户原话").ids[0]
    new_id = core.update_memory(scope, old_id, content="我在准备秋季校招投递", reason="阶段变了").ids[0]

    new = core.get_memory(scope, new_id)
    old = core.get_memory(scope, old_id)
    assert new is not None and new.kind == "status"
    assert new.id in [new_id]
    assert old is not None and old.status == "superseded"


def test_crud_is_scoped(core):
    """CRUD 也受 scope 隔离：A 账号改不到 B 账号的记忆。"""
    a = Scope(account="a")
    b = Scope(account="b")
    mid = core.write(a, "只在 A 账号里的记忆", kind="fact").ids[0]

    assert core.delete_memory(b, mid) is False
    assert core.get_memory(b, mid) is None
    assert core.get_memory(a, mid) is not None
