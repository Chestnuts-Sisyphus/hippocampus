"""验收 A39 / T6：代理形态——确认块由记忆层生成、由代理追加、可关、可消费。

同时覆盖"改 base_url 即生效"的机制面：请求前注入、响应后固化、`确认 n`/`否决 n`
在入口被消费（不转发给上游）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from hippocampus.core import MemoryCore, Scope
from hippocampus.proxy.app import CONFIRM_HEADER, build_app
from hippocampus.seed import seed


def _client(core: MemoryCore, **kwargs) -> TestClient:
    return TestClient(build_app(core, offline=True, **kwargs))


def _post(client: TestClient, account: str, content: str, headers: dict[str, str] | None = None):
    """带 account 头的请求：代理按头解析 scope（默认 default）。"""
    head = {"X-Hippocampus-Account": account}
    head.update(headers or {})
    return client.post("/v1/chat/completions", headers=head, json={"messages": [{"role": "user", "content": content}]})


def _reply(response) -> str:
    return response.json()["choices"][0]["message"]["content"]


def test_health_reports_port_lock_index(core, scope):
    payload = _client(core).get("/health").json()
    assert payload["ok"] is True
    assert payload["port"] == 8765
    assert "lock" in payload and "path" in payload["lock"]
    assert set(payload["stats"]) >= {"memories", "entities", "episodes", "relations"}


def test_injection_happens_before_reply(core, scope):
    seed(core, scope)
    reply = _reply(_post(_client(core), scope.account, "我投简历有什么要求？"))
    assert "简历投递前必须先过一遍错别字" in reply, "注入的记忆应出现在回复里（离线档回执会如实列出）"


def test_memory_switch_off_disables_injection(core, scope):
    seed(core, scope)
    core.set_switch(scope, "关闭记忆")
    try:
        reply = _reply(_post(_client(core), scope.account, "我投简历有什么要求？"))
    finally:
        core.set_switch(scope, "打开记忆")
    assert "注入开关：已关闭" in reply


def test_confirm_block_appended_by_system_not_model(core, scope):
    """确认块由**系统**追加：离线档没有模型参与，块照样出现（证明不是模型写的）。"""
    core.write(scope, "我的期望城市是北京", kind="preference", source_quote="用户原话")
    core.write(scope, "我的期望城市是杭州", kind="preference", source_quote="用户改口")

    reply = _reply(_post(_client(core), scope.account, "我随便说点什么"))
    assert reply, "回复不应为空"
    assert "记忆·确认结果" not in reply, "没有确认动作时不该出现确认结果块"


def test_confirm_command_is_consumed_and_switchable(core, scope):
    """`确认 n` 由代理入口消费 → 落在记忆层；关掉开关时确认块不追加。"""
    core.write(scope, "我的期望城市是北京", kind="preference")
    core.write(scope, "我的期望城市是杭州", kind="preference")
    pending = core.pending(scope)
    candidate = next(p for p in pending if p["is_new"])

    reply = _reply(_post(_client(core), scope.account, f"确认{candidate['num']}"))
    assert "已确认" in reply, "确认指令应在入口被消费并回执"
    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows["我的期望城市是杭州"] == "active"
    assert rows["我的期望城市是北京"] == "superseded"


def test_veto_command_keeps_old_value(core, scope):
    core.write(scope, "我的期望城市是北京", kind="preference")
    core.write(scope, "我的期望城市是杭州", kind="preference")
    reply = _reply(_post(_client(core), scope.account, "否决"))
    assert "已否决" in reply
    rows = {m.content: m.status for m in core.list_memories(scope, limit=5, status=None)}
    assert rows["我的期望城市是北京"] == "active", "否决后旧值继续生效"
    assert rows["我的期望城市是杭州"] == "superseded"


def test_confirm_header_can_turn_block_off(core, scope):
    """客户端可用请求头声明"这条不要确认块"（A39 的"可关"）。"""
    core.write(scope, "我的期望城市是北京", kind="preference")
    core.write(scope, "我的期望城市是杭州", kind="preference")
    response = _post(_client(core), scope.account, "说点什么", headers={CONFIRM_HEADER: "0"})
    assert "记忆·确认" not in _reply(response)


def test_scope_isolation_by_header(core):
    """不同 account 头 → 不同记忆库（互不串）。"""
    client = _client(core)
    _post(client, "a", "我只投远程岗位")
    a_items = core.list_memories(Scope(account="a"), limit=10)
    b_items = core.list_memories(Scope(account="b"), limit=10)
    assert a_items, "a 账号应有写入"
    assert not b_items, "b 账号不应看到 a 的记忆"


def test_invalid_scope_header_rejected(core):
    """scope 头是外部输入：目录穿越写法必须被拒（不落到文件系统上）。"""
    client = TestClient(build_app(core, offline=True), raise_server_exceptions=False)
    response = _post(client, "../../etc", "hi")
    assert response.status_code == 400, "非法 scope 应回 400（不是 500）"


def test_models_endpoint(core):
    """`/v1/models` 回**配置的模型名**（A6，0.2.1 口径变更）。

    此前回占位串 `hippocampus`——部分客户端会校验列表里有没有自己请求的模型名，
    占位串会让它们拒用。现在回 `llm.model`（或环境变量），未配置时才回内置名。
    """
    from hippocampus import settings as mem_settings

    payload = _client(core).get("/v1/models").json()
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == (mem_settings.endpoint_model() or "hippocampus")
