"""N20 安全审计收口：Mimosa 9 项逐条处置的测试锚点。

逐条加固对应的生产代码位置：
- ① SQL 标识符白名单：`memory/database.py::_safe_ident/_safe_ddl`（PRAGMA/ALTER 拼串前校验）；
- ② BM25 表名白名单：`memory/retrieval.py::_safe_table`（只允许 memories/episodes）；
- ③ embedding 下载出站校验：`memory/embedding_models.py::_safe_repo/_validate_model_url/set_http_fetcher`；
- ③′ `llm_proxy.call_upstream`（随迁备用路径）：出站前过 `validate_endpoint_url`（A41 同口径）。
"""

from __future__ import annotations

import pytest

from hippocampus.memory import database as db
from hippocampus.memory import embedding_models as em
from hippocampus.memory import retrieval as rt
from hippocampus.proxy import llm_proxy


def test_safe_ident_allows_normal_and_blocks_injection():
    assert db._safe_ident("memories") == "memories"  # noqa: SLF001
    assert db._safe_ident("last_hit_at") == "last_hit_at"  # noqa: SLF001
    for bad in ("memories; DROP TABLE x", "m--x", "m x", "", "0abc", "a-b"):
        with pytest.raises(ValueError):
            db._safe_ident(bad)  # noqa: SLF001


def test_safe_ddl_allows_column_defs_and_blocks_injection():
    assert db._safe_ddl("shadow INTEGER DEFAULT 0") == "shadow INTEGER DEFAULT 0"  # noqa: SLF001
    for bad in ("a); DROP TABLE memories;--", "a;SELECT", "", "x -- y"):
        with pytest.raises(ValueError):
            db._safe_ddl(bad)  # noqa: SLF001


def test_safe_table_allows_real_tables_only():
    assert rt._safe_table("memories") == "memories"  # noqa: SLF001
    assert rt._safe_table("episodes") == "episodes"  # noqa: SLF001
    for bad in ("sqlite_master", "memories;--", "", "MEMORIES"):
        with pytest.raises(ValueError):
            rt._safe_table(bad)  # noqa: SLF001


def test_safe_repo_allows_org_name_only():
    assert em._safe_repo("Xenova/bge-small-en-v1.5") == "Xenova/bge-small-en-v1.5"  # noqa: SLF001
    assert em._safe_repo("bge-small-en-v1.5") == "bge-small-en-v1.5"  # noqa: SLF001
    for bad in ("../../etc/passwd", "a/b/c", "x@evil.com:8080", "a b", "/abs/path", "org/../x", ""):
        with pytest.raises(ValueError):
            em._safe_repo(bad)  # noqa: SLF001


def test_validate_model_url_pins_host_to_endpoint():
    good = em._validate_model_url(  # noqa: SLF001
        "https://hf-mirror.com/api/models/Xenova/bge-small-en-v1.5/tree/main", "https://hf-mirror.com"
    )
    assert good.startswith("https://hf-mirror.com/")
    with pytest.raises(ValueError, match="不一致"):
        em._validate_model_url("https://evil.example.com/x", "https://hf-mirror.com")  # noqa: SLF001
    with pytest.raises(ValueError):  # 非 http/https
        em._validate_model_url("ftp://hf-mirror.com/x", "ftp://hf-mirror.com")  # noqa: SLF001
    with pytest.raises(ValueError):  # 环回地址：模型端点表里没有它 → 拒绝
        em._validate_model_url("http://127.0.0.1:11434/x", "http://my-mirror.local")  # noqa: SLF001


def test_injected_fetcher_receives_validated_url():
    """注入式抓取器收到的是**已校验**的 URL（Mimosa 要求的"验证后注入 fetcher"写法）。"""
    seen: dict[str, str] = {}
    def _fake(url: str, timeout: int) -> bytes:
        seen["url"] = url
        return b"{}"

    em.set_http_fetcher(_fake)  # noqa: SLF001
    try:
        out = em._http_get("https://hf-mirror.com/api/models/x/tree/main", timeout=1)
    finally:
        em.set_http_fetcher(None)  # noqa: SLF001
    assert out == b"{}" and seen["url"].startswith("https://hf-mirror.com/")


def test_call_upstream_validates_url(monkeypatch):
    """备用路径也过出站校验：非法 scheme 直接抛，不发请求。"""
    with pytest.raises(ValueError):  # 只允许 http/https
        llm_proxy.call_upstream({}, "ftp://example.com/x", {}, {}, timeout=1)
    monkeypatch.setattr("hippocampus.proxy.llm_proxy.httpx.post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("不应发出")))
    monkeypatch.setattr(
        "hippocampus.proxy.llm_proxy.httpx.post",
        lambda url, json=None, headers=None, timeout=None: type("R", (), {"status_code": 200, "json": lambda self: {}})(),
    )
    status, _body = llm_proxy.call_upstream({}, "https://example.com/v1", {}, {})
    assert status == 200
