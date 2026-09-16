"""测试公共 fixture。

两条硬约定：
1. **测试环境无凭据**：autouse fixture 会清掉所有已知的 API key 环境变量，并把
   `HIPPOCAMPUS_OFFLINE` 置 1——这样 CI 与本地跑的是同一条"无 key"路径（验收 A36），
   任何"偷偷需要模型"的代码都会在这里暴露（`llm.available()` 为 False）。
2. **每个测试一个独立数据根**：不碰用户真实记忆库（`~/.hippocampus`）。
"""

from __future__ import annotations

import os

import pytest

from hippocampus.core import MemoryCore, Scope

API_KEY_ENV_VARS = (
    "HIPPOCAMPUS_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HIPPOCAMPUS_BASE_URL",
    "OPENAI_BASE_URL",
    "HIPPOCAMPUS_MODEL",
    "OPENAI_MODEL",
    "HIPPOCAMPUS_SECRET_CMD",
)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    """所有测试默认在"无凭据 + 离线"下跑。"""
    for name in API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HIPPOCAMPUS_OFFLINE", "1")
    yield


@pytest.fixture(autouse=True)
def _clean_embedding_cache():
    """清掉检索层的模块级缓存（跨测试串味会让向量空间对不上）。

    检索层把 embedding 函数缓存在模块全局（`_EMB_FN` / `_EMB_FN_MODEL`）和 LRU 里。
    某个测试把它替换/回退过之后，后续测试会用**同一个维度的另一个模型**（384 维，
    维数对得上、空间对不上）去查另一个测试建的集合——分数会静默变成垃圾。
    这类问题只会表现为"单独跑过、一起跑挂"，所以按测试隔离。
    """
    from hippocampus.memory import retrieval as rt

    rt._EMB_FN = None  # noqa: SLF001
    rt._EMB_FN_MODEL = None  # noqa: SLF001
    try:
        rt._query_embedding_cached.cache_clear()
    except AttributeError:
        pass
    yield
    rt._EMB_FN = None  # noqa: SLF001
    rt._EMB_FN_MODEL = None  # noqa: SLF001
    try:
        rt._query_embedding_cached.cache_clear()
    except AttributeError:
        pass


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """独立数据根。"""
    path = tmp_path / "hippo_home"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HIPPOCAMPUS_HOME", str(path))
    return path


@pytest.fixture()
def core(home):
    core = MemoryCore(home=home)
    yield core
    core.close()


@pytest.fixture()
def scope():
    return Scope(account="test", session="s1", source="user")


@pytest.fixture()
def workdir(tmp_path):
    path = tmp_path / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def sealed_env(monkeypatch):
    """显式断言"没有任何凭据通道"的测试用：连 keyring 也挡掉。"""
    import hippocampus.memory.config as cfg

    monkeypatch.setattr(cfg, "_read_from_keyring", lambda: "")
    monkeypatch.delenv("HIPPOCAMPUS_HOME", raising=False)
    return os.environ
