"""存储后端协议（F5/A21）：`MemoryCore` 只依赖协议，SQLite＋Chroma 是**默认实现**。

方案里的"可插拔三点"（记忆后端／工具来源／模型端点）在本轮落地第一点：
- `MemoryBackend.open_session(account, data_dir)` 返回一个会话对象；
- 会话对象要实现 `SessionBackend` 协议里的方法（MemoryCore 用到的全部）；
- 换后端＝实现同一组方法并在 `MemoryCore(backend=...)` 传入，**不改核心代码**。

默认实现 `SqliteChromaBackend` 就是既有实现（SQLite 主库 + Chroma 双池向量集合）；
`vector=False` 时走词法降级会话（无向量库环境与 CI 用），也是同一个后端类的一个档位。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from hippocampus.memory import database as db
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt


@runtime_checkable
class SessionBackend(Protocol):
    """一个账户会话的最小接口（MemoryCore 依赖的全部方法/属性）。

    不用继承：任何实现这组方法的对象都算（结构化子类型），便于第三方后端接入。
    """

    account_id: str
    data_dir: Path
    lock: Any
    conn: Any
    collections: dict
    idx: Any
    pending_block: Any
    pending_blocks: list

    def reindex(self) -> None: ...
    def close(self) -> None: ...
    def push_pending_block(self, block: Any) -> None: ...
    def pop_pending_block(self, block: Any) -> None: ...
    def match_confirmation(self, user_text: str) -> Any: ...
    def maybe_run_maintenance(self, now: int | None = None) -> dict: ...


@runtime_checkable
class MemoryBackend(Protocol):
    """存储后端：按账户打开会话。"""

    name: str

    def open_session(self, account_id: str, data_dir: Path) -> SessionBackend: ...


def lexical_session(account_id: str, data_dir: Path) -> mb.MemorySession:
    """无向量档会话：不建 chroma 集合（语义通道天然空），其余照常。

    前身把"没有向量库"当硬依赖；这里把它降级成一个档位——BM25／图／事件线索
    ＋完全去重仍然可用，`doctor` 与 stderr 明说。
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    session = mb.MemorySession.__new__(mb.MemorySession)
    session.account_id = account_id
    session.data_dir = data_dir
    session.db_path = data_dir / "memory.db"
    session.chroma_dir = data_dir / "chroma"
    session.lock = threading.RLock()
    conn = sqlite3.connect(str(session.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # [HIPPO] 九轮 W11：这里原先手抄了一份"到 security 为止"的序列，漏了求证四列与
    # pending_blocks——老库经词法档打开后一写就 `no column named verification_status`。
    # 与 `MemorySession` 同走 `apply_migrations` 这唯一入口（`database.py` 的注释早就写了"以后
    # 新增 ensure_* 只改这一处"，本档正是那条注释说的漂移）。
    db.apply_migrations(conn)
    session.conn = conn
    session.collections = {"mem": None, "ep": None}
    session.collection = None
    session.idx = rt.build_bm25(conn)
    session.pending_block = None
    session.pending_blocks = []
    session._pending_alarm = None
    session._last_lifecycle_scan_ms = 0
    session._LIFECYCLE_SCAN_INTERVAL_MS = 24 * 3600 * 1000
    session._last_maintenance_scan_ms = 0
    session._MAINTENANCE_SCAN_INTERVAL_MS = 24 * 3600 * 1000
    session._episode_by_text = {}
    session._EP_CACHE_MAX = 20
    session._index_error = ""
    return session


class SqliteChromaBackend:
    """默认后端：SQLite（主库）＋ Chroma（双池向量集合）。

    `vector=False` → 词法降级档（不建向量集合；同一后端类的档位，不是另一条代码路径）。
    """

    name = "sqlite+chroma"

    def __init__(self, *, vector: bool = True) -> None:
        self.vector = bool(vector)

    def open_session(self, account_id: str, data_dir: Path) -> SessionBackend:
        if not self.vector:
            return lexical_session(account_id, data_dir)
        return mb.MemorySession(account_id, data_dir=Path(data_dir))


__all__ = ["MemoryBackend", "SessionBackend", "SqliteChromaBackend", "lexical_session"]
