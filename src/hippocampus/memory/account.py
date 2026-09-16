"""scope → 存储目录解析（前身 account.py 的本项目版本）。

前身 `account.py` 兼管注册/登录/令牌/元数据库/LLM 配置；本项目**不做账号体系**
（见《用户视角全流程》§7"不做什么"），只保留 scope 解析这一件事：

    data_root/accounts/<account_id>/
        memory.db     # SQLite 主库（实体/记忆/经历/关系/参数快照）
        chroma/       # 向量库（双池 mem / ep）
        observe.jsonl # 观测日志

安全：`account_id` 来自外部输入（HTTP 头 / CLI 参数），必须做**目录穿越防护**——
只允许 `[A-Za-z0-9._-]`，且拒绝 `..`、空串、绝对路径、超过 64 字符。
"""

from __future__ import annotations

import re
from pathlib import Path

from hippocampus.memory import runtime

DEFAULT_ACCOUNT = "default"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InvalidScopeError(ValueError):
    """scope 标识非法（目录穿越/超长/空）。"""


def is_valid_account_id(account_id: str) -> bool:
    if not isinstance(account_id, str):
        return False
    if not _ID_RE.match(account_id):
        return False
    return ".." not in account_id


def safe_account_id(account_id: str | None) -> str:
    """校验并归一 account_id；非法直接拒绝（不静默改写，避免撞库）。"""
    if account_id is None or account_id == "":
        return DEFAULT_ACCOUNT
    if not is_valid_account_id(account_id):
        raise InvalidScopeError(f"非法 scope 标识: {account_id!r}（只允许 [A-Za-z0-9._-]，≤64 字符，且不含 ..）")
    return account_id


def get_account_data_dir(account_id: str | None = None) -> Path:
    """返回该 scope 的数据目录（不创建）。"""
    return runtime.data_root() / "accounts" / safe_account_id(account_id)


def ensure_account_data_dir(account_id: str | None = None) -> Path:
    path = get_account_data_dir(account_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_accounts() -> list[str]:
    """已有记忆库的 scope 列表（CLI `memory list --scopes` 用）。"""
    base = runtime.data_root() / "accounts"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "memory.db").exists())
