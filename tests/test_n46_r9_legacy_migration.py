"""九轮 W11（K16）—— 老库迁移真机测试：仓库内派生 legacy 夹具。

为什么不在仓库里放一份 `.db` 二进制老库：放二进制＝把**某一版结构复制**进仓库，
正本 `SCHEMA` 一改它就悄悄过期，测的永远是假老库。这里的夹具由现行 `database.SCHEMA`
**当场减去**后加的列与后建的表派生出来——正本演进，夹具跟着演进（差集仍是"当年缺的那些"），
零复制、零漂移、零本机绝对路径（全部落在 pytest 的 `tmp_path`／`home` 里）。

「schema_version 前后断言可见」的口径订正（改判，登记见 docs/roadmap.md 九轮 W11 行）：
库内**没有**版本号字段（`PRAGMA user_version` 全程未使用；补它＝动 accounts schema，撞硬边界），
所以"前后"用**可见的结构差异**断言：迁移前缺的列与表，迁移后必须在，且老行照读不误。
真正的版本号在导出包 manifest（`core/transfer.py: SCHEMA_VERSION`），跨版本拒绝文案另有一条测试钉死。
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from hippocampus.core import MemoryCore, Scope, transfer
from hippocampus.memory import account as account_mod
from hippocampus.memory import database as db

# 老库**没有**、迁移必须补回来的东西（后加列：B2 四列／P04 security_flag／七轮求证四列）
LEGACY_DROPPED_COLUMNS = (
    "is_hub",
    "lifecycle",
    "last_hit_at",
    "shadow",
    "security_flag",
    "verification_status",
    "verification_method",
    "verified_at",
    "verification_evidence",
)
# 后建表（反馈环 N1 / 第三批快照 / 待确认块）：老库里没有，`apply_migrations` 必须建出来
LEGACY_DROPPED_TABLES = ("feedback_logs", "param_versions", "param_snapshots")

# 老库里种一条 v1 形状的记忆用的列（全部是 v1 就有的，不碰后加列）
_V1_MEMORY_COLS = (
    "id",
    "type",
    "status",
    "entity_ids",
    "scene_tags",
    "scene_description",
    "content",
    "content_lemmatized",
    "source_quote",
    "source_episode_id",
    "change_context",
    "created_at",
    "updated_at",
)
LEGACY_CONTENT = "我只投允许远程的岗位"
LEGACY_ID = "mem_legacy0001"


def _legacy_schema() -> str:
    """从现行 `SCHEMA` 派生"当年那一版"：删掉后加的列行与后建的表（含其索引）。"""
    out: list[str] = []
    skipping_table = False
    for line in db.SCHEMA.splitlines():
        stripped = line.strip()
        created = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", stripped)
        if created:
            skipping_table = created.group(1) in LEGACY_DROPPED_TABLES
        if skipping_table:
            if stripped.startswith(");"):
                skipping_table = False
            continue
        indexed = re.search(r"\bON (\w+)\(", stripped)
        if indexed and indexed.group(1) in LEGACY_DROPPED_TABLES:
            continue
        column = re.match(r"(\w+)\s+\w", stripped)
        if column and column.group(1) in LEGACY_DROPPED_COLUMNS:
            continue
        out.append(line)
    return "\n".join(out)


def _build_legacy_db(path) -> None:
    """按老结构建库，并种一条老行（真文件、真 SQLite，不走任何 mock）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_legacy_schema())
    conn.execute(
        f"INSERT INTO memories ({', '.join(_V1_MEMORY_COLS)}) "
        f"VALUES ({', '.join('?' * len(_V1_MEMORY_COLS))})",
        (
            LEGACY_ID,
            "preference",
            "active",
            "[]",
            "[]",
            "",
            LEGACY_CONTENT,
            "",
            "",
            "",
            "",
            1_700_000_000_000,
            1_700_000_000_000,
        ),
    )
    conn.commit()
    conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _structure(conn: sqlite3.Connection) -> dict[str, set[str]]:
    return {t: _columns(conn, t) for t in sorted(_tables(conn))}


def test_夹具确实是老库_与现行正本必须有差集():
    """防空转对照：夹具若悄悄长成现行结构（差集为空），下面的迁移断言就全成空话。"""
    legacy = _legacy_schema()
    for col in LEGACY_DROPPED_COLUMNS:
        assert re.search(rf"^\s*{col}\s", legacy, re.MULTILINE) is None, f"夹具里还留着后加列 {col}"
        assert re.search(rf"^\s*{col}\s", db.SCHEMA, re.MULTILINE), f"正本已无后加列 {col}——请同步差集清单"
    for table in LEGACY_DROPPED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in legacy, f"夹具里还留着后建表 {table}"
        assert f"CREATE TABLE IF NOT EXISTS {table}" in db.SCHEMA, f"正本已无 {table}——请同步差集清单"


def test_老库一打开就迁移_补列补表且老行不丢(tmp_path):
    path = tmp_path / "legacy" / "memory.db"
    _build_legacy_db(path)

    raw = sqlite3.connect(str(path))
    try:
        before_cols = _columns(raw, "memories")
        before_tables = _tables(raw)
        assert "verification_status" not in before_cols and "lifecycle" not in before_cols
        assert "pending_blocks" not in before_tables and "param_snapshots" not in before_tables
    finally:
        raw.close()

    conn = db.connect(path)
    try:
        after_cols = _columns(conn, "memories")
        for col in ("lifecycle", "last_hit_at", "shadow", "security_flag", "verification_status"):
            assert col in after_cols, f"迁移后仍缺列 {col}"
        after_tables = _tables(conn)
        for table in (*LEGACY_DROPPED_TABLES, "pending_blocks"):
            assert table in after_tables, f"迁移后仍缺表 {table}"
        row = conn.execute(
            "SELECT content, lifecycle, shadow, security_flag, verification_status FROM memories WHERE id=?",
            (LEGACY_ID,),
        ).fetchone()
        assert row[0] == LEGACY_CONTENT, "老行内容丢了"
        assert row[1:] == ("active", 0, 0, "unverifiable"), "老行没吃到新列的默认值"
    finally:
        conn.close()


def test_迁移幂等_再开一次结构一字不差(tmp_path):
    path = tmp_path / "legacy" / "memory.db"
    _build_legacy_db(path)

    conn = db.connect(path)
    first = _structure(conn)
    conn.close()
    conn2 = db.connect(path)
    second = _structure(conn2)
    rows = conn2.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    snapshot = conn2.execute("SELECT COUNT(*) FROM param_snapshots WHERE is_active=1").fetchone()[0]
    conn2.close()

    assert first == second, "二次打开结构发生变化＝迁移不幂等"
    assert rows == 1, "重复迁移把老行翻倍"
    assert snapshot == 1, "活跃参数快照被重复种（seed 不幂等）"


def test_老库经核心读写一通_词法档不依赖向量库(home, tmp_path, scope):
    """真机路径：老库落在 `home/accounts/<safe_id>/memory.db`，由 MemoryCore 打开→迁移→读写。"""
    account = "legacy"
    data_dir = home / "accounts" / account_mod.safe_account_id(account)
    _build_legacy_db(data_dir / "memory.db")

    core = MemoryCore(home=home, vector=False)
    legacy_scope = Scope(account=account, session="s1", source="user")
    try:
        listed = {m.id for m in core.list_memories(legacy_scope, limit=50, include_shadow=True)}
        assert LEGACY_ID in listed, "迁移后的老记忆在核心层读不到"
        written = core.write(legacy_scope, "目标城市是天津", kind="preference", explicit=True)
        assert written.created == 1 and written.ids, f"新记忆没写进去：{written.note}"
        new_id = written.ids[0]
        listed_after = {m.id for m in core.list_memories(legacy_scope, limit=50, include_shadow=True)}
        assert {LEGACY_ID, new_id} <= listed_after, "写入后老行被挤掉"
        hit = core.search(legacy_scope, "天津", limit=8)
        assert new_id in {i.id for i in hit.items}, f"迁移后的库检索不到新写入：{hit.channels}"
        assert core.stats(legacy_scope)["memories"] == 2, "后加的列没被写入/统计路径用上"
    finally:
        core.close()
    raw = sqlite3.connect(str(data_dir / "memory.db"))
    try:
        assert "verification_status" in _columns(raw, "memories")
        assert raw.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    finally:
        raw.close()


def test_两档迁移终点必须一致_防再各抄一份序列(tmp_path):
    """九轮 W11 实测到的缺陷钉死：词法档曾手抄一份停在 security 的过时序列。

    向量档（`MemorySession`）走 `apply_migrations`，词法档（`lexical_session`）当年自己抄了一遍
    ——正本加列时只有一档跟着长，老库在另一档里就写不进去。两档终点结构必须一字不差。
    """
    from hippocampus.core import backend as backend_mod
    from hippocampus.memory import memory_bridge as mb

    structures: dict[str, dict[str, set[str]]] = {}
    for label, opener in (
        ("向量档", lambda d: mb.MemorySession("mig", data_dir=d)),
        ("词法档", lambda d: backend_mod.lexical_session("mig", d)),
    ):
        data_dir = tmp_path / label
        data_dir.mkdir(parents=True)
        _build_legacy_db(data_dir / "memory.db")
        session = opener(data_dir)
        try:
            structures[label] = _structure(session.conn)
        finally:
            session.conn.close()

    assert structures["向量档"] == structures["词法档"], (
        "两档迁移终点不一致＝有一条序列被各抄了一份；只允许走 `database.apply_migrations`"
    )
    for label, struct in structures.items():
        assert "verification_status" in struct["memories"], f"{label} 漏了求证四列"
        assert "pending_blocks" in struct, f"{label} 漏了 pending_blocks 表"


def test_跨版本导入被拒_文案必须能照着做(tmp_path, home, scope):
    """拒绝路径给**可执行**文案：两个版本号都在、并指出迁移指引在哪；不是含糊一句"版本不符"。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True)
    other = transfer.SCHEMA_VERSION + 7
    (pkg / "manifest.json").write_text(
        json.dumps({"schema_version": other, "app_version": "0.0.0", "account": scope.account}), encoding="utf-8"
    )
    core = MemoryCore(home=home, vector=False)
    try:
        with pytest.raises(ValueError) as exc:
            transfer.import_package(core, scope, pkg)
    finally:
        core.close()
    msg = str(exc.value)
    assert str(other) in msg and str(transfer.SCHEMA_VERSION) in msg, f"文案没给两个版本号：{msg}"
    assert "CHANGELOG" in msg, f"文案没指迁移指引在哪：{msg}"
    assert not (home / "accounts" / account_mod.safe_account_id(scope.account) / "memory.db").exists(), (
        "被拒之前就把目标库落盘了——拒绝必须发生在写盘之前"
    )


def test_同版本放行_拒绝闸不是常闭(home, scope, tmp_path):
    """对照：同一个 `SCHEMA_VERSION` 的包必须能导入（否则上一条测试会因"永远抛"而假绿）。"""
    core = MemoryCore(home=home, vector=False)
    core.write(scope, "我只投允许远程的岗位", kind="preference", explicit=True)
    pkg = tmp_path / "pkg"
    manifest = transfer.export_package(core, scope, pkg)
    assert manifest["schema_version"] == transfer.SCHEMA_VERSION
    core.close()

    home2 = tmp_path / "home2"
    core2 = MemoryCore(home=home2, vector=False)
    dst = Scope(account="restored", session="s1", source="user")
    try:
        result = transfer.import_package(core2, dst, pkg)
        assert result["stats"]["memories"] == 1, "同版本包被误拒或导入丢数据"
    finally:
        core2.close()
