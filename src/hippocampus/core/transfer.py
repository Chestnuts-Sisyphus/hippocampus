"""记忆库导出/导入（F4）：目录包＝`manifest.json`（含 schema 版本）＋ `memory.db`。

设计取舍：
- **只导出源真相**（SQLite 主库）：向量索引／BM25 都是**派生物**，导入端从主库重建，
  不搬索引文件（搬索引＝搬一致性风险，还要求两边 chroma 版本一致）。
- 导出用 `VACUUM INTO` 做**一致快照**（SQLite 官方做法；不需要停写、不依赖复制时序）。
- 导入默认**不覆盖**已存在的库（`--force` 才覆盖）；覆盖时旧向量目录改名保留
  （`chroma.bak-<时间戳>`），不物理删除。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from hippocampus.memory import database as db

# 主库结构版本（与 MemoryCore 接口 v1 相配；结构破坏性变更时 +1 并在 CHANGELOG 写迁移指引）
SCHEMA_VERSION = 1


def export_package(core: Any, scope: Any, target_dir: str | Path, *, app_version: str = "") -> dict[str, Any]:
    """把某个 scope 的记忆库导出为目录包。返回 manifest（同时写入 manifest.json）。"""
    from hippocampus import __version__

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    session = core._session(scope)  # noqa: SLF001 - 与核心同仓的内部工具
    dst_db = target / "memory.db"
    with session.lock:
        if dst_db.exists():
            dst_db.unlink()
        # 一致快照（VACUUM INTO；写事务并发下也得到完整副本）
        session.conn.execute("VACUUM INTO ?", (str(dst_db),))
        counts = core.stats(scope)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "app_version": app_version or __version__,
        "created_at": db.now_ms(),
        "account": scope.account,
        "counts": counts,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def read_manifest(package_dir: str | Path) -> dict[str, Any]:
    """读目录包 manifest（缺失/损坏 → 抛 ValueError）。"""
    path = Path(package_dir) / "manifest.json"
    if not path.exists():
        raise ValueError(f"不是有效的导出包（缺 manifest.json）: {package_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ValueError(f"manifest.json 损坏: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("manifest.json 顶层应为对象")
    return data


def import_package(core: Any, scope: Any, package_dir: str | Path, *, force: bool = False) -> dict[str, Any]:
    """把目录包导入某个 scope 的库。返回 {manifest, stats}。

    - schema 版本不符 → 拒绝（拒绝比"猜着读"安全）；
    - 目标库已存在且非 force → 拒绝；
    - 覆盖时旧向量目录改名保留（`chroma.bak-<ts>`），导入后从主库重建索引。
    """
    manifest = read_manifest(package_dir)
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError(
            f"导出包 schema 版本 {manifest.get('schema_version')} 与当前 {SCHEMA_VERSION} 不符；"
            "请用对应版本的 Hippocampus 导入（迁移指引见 CHANGELOG）"
        )
    src_db = Path(package_dir) / "memory.db"
    if not src_db.exists():
        raise ValueError(f"导出包缺 memory.db: {package_dir}")

    data_dir = core._data_dir(scope.account)  # noqa: SLF001
    data_dir.mkdir(parents=True, exist_ok=True)
    dst_db = data_dir / "memory.db"
    if dst_db.exists() and not force:
        raise FileExistsError(f"目标库已存在（加 --force 覆盖）: {dst_db}")

    # 先关掉当前会话（释放 sqlite/chroma 句柄），再落盘
    with core._registry_guard:  # noqa: SLF001
        old = core._sessions.pop(scope.account, None)  # noqa: SLF001
    if old is not None:
        try:
            old.close()
        except Exception:
            pass

    if dst_db.exists():
        backup_db = data_dir / f"memory.db.bak-{int(time.time())}"
        shutil.copy2(dst_db, backup_db)  # 覆盖前留旧副本（不物理删除）
    shutil.copy2(src_db, dst_db)

    # 向量索引是派生物：旧目录改名保留，然后从主库重建
    chroma_dir = data_dir / "chroma"
    if force and chroma_dir.exists():
        bak = data_dir / f"chroma.bak-{int(time.time())}"
        try:
            chroma_dir.rename(bak)
        except OSError:
            pass
    session = core._session(scope)  # noqa: SLF001
    with session.lock:
        session.reindex()
    return {"manifest": manifest, "stats": core.stats(scope)}


__all__ = ["SCHEMA_VERSION", "export_package", "import_package", "read_manifest"]
