"""数据/资源根目录解析（前身 runtime.py 的本项目版本）。

前身用**单一全局 data_root**（打包版写 LOCALAPPDATA，源码版写代码目录）。本项目要
同时满足三件事：① 两个形态共用一份记忆库；② 多 scope 并行（代理线程 + Agent 线程）；
③ CI 与测试要能指向临时目录。因此改为**可显式设置的根目录**：

    显式 set_data_root() > 环境变量 HIPPOCAMPUS_HOME > 默认 ~/.hippocampus

`resource_root()` 固定指向本包目录（内置资源，只读）。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

_ENV_VAR = "HIPPOCAMPUS_HOME"

_lock = threading.RLock()
_explicit_root: Path | None = None
_stack: list[Path | None] = []


def default_data_root() -> Path:
    """环境变量优先，否则 ~/.hippocampus。"""
    env = os.environ.get(_ENV_VAR, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".hippocampus").resolve()


def data_root() -> Path:
    with _lock:
        if _explicit_root is not None:
            return _explicit_root
    return default_data_root()


def set_data_root(path: str | Path | None) -> None:
    """进程级设置数据根（None = 回到默认解析）。"""
    global _explicit_root
    with _lock:
        _explicit_root = Path(path).expanduser().resolve() if path else None


@contextmanager
def using_data_root(path: str | Path):
    """临时切换数据根（可嵌套，退出恢复）。测试与多 scope 并行用。"""
    global _explicit_root
    with _lock:
        _stack.append(_explicit_root)
        _explicit_root = Path(path).expanduser().resolve()
    try:
        yield _explicit_root
    finally:
        with _lock:
            _explicit_root = _stack.pop() if _stack else None


def resource_root() -> Path:
    """内置资源目录（本包目录）。"""
    return Path(__file__).resolve().parent


def ensure_data_dir() -> Path:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def models_dir() -> Path:
    """本地模型目录（embedding 两级默认的推荐档落地处）。"""
    path = data_root() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path
