"""验收 A40 / A41 相关的安全与并发测试。

- A40：单写者保护（第二个写者被拒或排队；僵尸锁可回收）
- A41：出站 URL 校验（仅 http/https；拒环回／私有／保留）
- 凭据：配置里不许出现凭据字段；凭据只从环境变量／密钥服务读

说明：本文件里所有"密钥"字样都是**占位符**（形如 placeholder-xxx），不是可用凭据；
测试要验的是"配置里出现凭据字段会被拒"这条规则本身。
"""

from __future__ import annotations

import json

import pytest

from hippocampus.core import MemoryCore, Scope
from hippocampus.core.locks import WriterBusy, WriterLock
from hippocampus.net import UnsafeURLError, validate_endpoint_url, validate_outbound_url

PLACEHOLDER_KEY = "placeholder-not-a-real-credential"


# ---------------------------------------------------------------- A40


def test_second_writer_is_rejected_or_queued(home):
    """同一库目录：第二个写者拿不到锁（短超时下被拒）。"""
    first = WriterLock(home / "accounts" / "test", timeout_s=1.0)
    second = WriterLock(home / "accounts" / "test", timeout_s=0.2)
    first.acquire()
    try:
        assert second.is_held_by_other() is True
        with pytest.raises(WriterBusy):
            second.acquire()
    finally:
        first.release()


def test_lock_is_reentrant_within_process(home):
    """同实例可重入（调用链里会重复获取，不能自锁死）。"""
    lock = WriterLock(home / "accounts" / "test", timeout_s=1.0)
    with lock.held():
        with lock.held():
            assert lock.held_by_self is True
        assert lock.held_by_self is True
    assert lock.held_by_self is False


def test_zombie_lock_is_reclaimed(home):
    """进程死掉留下的锁文件：无人持锁 + 心跳过期 → 判为僵尸并可回收。"""
    data_dir = home / "accounts" / "test"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = data_dir / ".writer.lock"
    lock_file.write_text(json.dumps({"pid": 999999, "ts": 0.0}), encoding="utf-8")

    lock = WriterLock(data_dir, timeout_s=0.5)
    assert lock.is_stale(stale_after_s=60) is True
    assert lock.force_release(stale_after_s=60) is True
    assert not lock_file.exists()

    core = MemoryCore(home=home)
    try:
        result = core.write(Scope(account="test"), "僵尸锁回收后仍可写", kind="fact")
        assert result.ids
    finally:
        core.close()


def test_live_lock_is_not_reclaimed(home):
    """活进程持有的锁不许被"强行回收"（避免踩掉正在写的进程）。"""
    data_dir = home / "accounts" / "test"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = WriterLock(data_dir, timeout_s=1.0)
    lock.acquire()
    try:
        other = WriterLock(data_dir, timeout_s=0.2)
        assert other.is_stale(stale_after_s=0.0001) is False
        assert other.force_release(stale_after_s=0.0001) is False
    finally:
        lock.release()


def test_doctor_reports_lock(core, scope):
    """doctor 能查到锁状态（含"被谁持有/是否僵尸"）。"""
    status = core.lock_status(scope)
    assert set(status) >= {"path", "held_by_self", "held_by_other", "stale", "meta"}


# ---------------------------------------------------------------- A41


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据地址
        "http://0.0.0.0/",
        "http://2130706433/",       # 127.0.0.1 的十进制写法
        "http://0x7f000001/",       # 十六进制写法
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "http://metadata.google.internal/",
    ],
)
def test_outbound_url_rejects_unsafe(url):
    """数据/模型提供的 URL：环回、私有、保留、非 http(s) 一律拒绝。"""
    with pytest.raises(UnsafeURLError):
        validate_outbound_url(url)


@pytest.mark.parametrize("url", ["https://example.com/a", "http://example.com:8080/b"])
def test_outbound_url_allows_public_http(url):
    assert validate_outbound_url(url) == url


def test_endpoint_url_allows_loopback_but_not_other_schemes():
    """操作员配置的端点：允许环回（本地模型合法），但仍然只允许 http/https。"""
    assert validate_endpoint_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    with pytest.raises(UnsafeURLError):
        validate_endpoint_url("file:///tmp/model")


def test_fetch_tool_uses_url_gate(core, scope, workdir):
    """工具层：抓取内网地址被拒（分类=被拒），不会真的发请求。"""
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=True, url_fetcher=lambda _u: "should-not-be-called")
    result = box.call("fetch_url", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert result["ok"] is False
    assert result["category"] == "被拒"


def test_tool_requires_confirmation_for_dangerous_action(core, scope, workdir):
    """危险动作（写文件）未授权时被拒，授权后才执行。"""
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=False)
    denied = box.call("write_file", {"path": "x.md", "content": "hi"})
    assert denied["ok"] is False and denied["category"] == "被拒"
    assert not (workdir / "x.md").exists()

    allowed = ToolBox(core, scope, workdir=workdir, allow_write=True)
    assert allowed.call("write_file", {"path": "x.md", "content": "hi"})["ok"] is True
    assert (workdir / "x.md").read_text(encoding="utf-8") == "hi"


def test_tool_rejects_path_escape(core, scope, workdir):
    """写文件工具不许越出工作目录。"""
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=True)
    result = box.call("write_file", {"path": "../../evil.md", "content": "x"})
    assert result["ok"] is False
    assert result["category"] == "参数错"


def test_tool_arg_validation_before_call(core, scope, workdir):
    """参数调用前校验：缺必填 / 类型错 / 枚举外 → 参数错，且不进函数体。"""
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=True)
    assert box.call("search_memory", {})["category"] == "参数错"
    assert box.call("search_memory", {"query": 123})["category"] == "参数错"
    assert box.call("remember", {"content": "x", "kind": "不存在的类型"})["category"] == "参数错"


def test_tool_health_view_opens_after_failures(core, scope, workdir):
    """健康视图：连续失败到阈值 → 熔断打开（编排层据此换路）。"""
    from hippocampus.agent.tools import ToolBox

    box = ToolBox(core, scope, workdir=workdir, allow_write=True)
    for _ in range(3):
        box.call("remember", {"content": "x", "kind": "非法"})
    health = box.health_view()
    assert "remember" in health["open"]


# ---------------------------------------------------------------- 凭据


def test_config_rejects_credential_fields(home, monkeypatch):
    """配置文件里出现疑似凭据字段 → 拒绝加载（fail loud）。"""
    from hippocampus.memory import config as mem_config

    bad = {"llm": {"api_key": PLACEHOLDER_KEY}}
    (home / "config.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="凭据"):
        mem_config.get_config()


def test_llm_config_reads_key_only_from_env(home, monkeypatch):
    """凭据只从环境变量读；配置里没有 key 也不影响读出端点与模型。"""
    from hippocampus.memory import config as mem_config

    monkeypatch.setenv("HIPPOCAMPUS_API_KEY", PLACEHOLDER_KEY)
    cfg = mem_config.get_llm_config()
    assert cfg["api_key"] == PLACEHOLDER_KEY
    assert cfg["base_url"]

    monkeypatch.delenv("HIPPOCAMPUS_API_KEY", raising=False)
    assert mem_config.get_llm_config()["api_key"] == ""


def test_offline_env_reflects_in_config(home, monkeypatch):
    from hippocampus.memory import config as mem_config

    monkeypatch.setenv("HIPPOCAMPUS_OFFLINE", "1")
    assert mem_config.is_offline() is True
    monkeypatch.setenv("HIPPOCAMPUS_OFFLINE", "0")
    monkeypatch.setattr(mem_config, "get_config", lambda: {"offline": {"enabled": False}})
    assert mem_config.is_offline() is False


def test_scope_id_rejects_path_escape(core):
    """scope 标识来自外部输入（HTTP 头/CLI 参数）→ 目录穿越必须被拒。"""
    from hippocampus.memory.account import InvalidScopeError

    with pytest.raises(InvalidScopeError):
        core.stats(Scope(account="../../etc"))
    with pytest.raises(InvalidScopeError):
        core.stats(Scope(account="a/b"))
    with pytest.raises(InvalidScopeError):
        core.stats(Scope(account=".."))
    with pytest.raises(InvalidScopeError):
        core.stats(Scope(account="x" * 100))
