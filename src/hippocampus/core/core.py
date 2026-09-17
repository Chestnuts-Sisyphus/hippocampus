"""`MemoryCore` —— 记忆核心 v1（冻结接口，见 docs/memory-core-v1.md）。

两种形态（代理 / Agent）共用本类，**不直接接触记忆层内部模块**。

接口纪律（验收 T3 由 scripts/check_interface.py 自动检查）：
1. 公开方法**第一参数恒为 `Scope`**；
2. 公开方法签名与 `types.py` 的类型里**不出现 HTTP 概念**（request／body／header／
   status_code／messages／endpoint 等），因为记忆语义与传输协议无关；
3. v1 **只允许追加字段**（新字段必须有默认值），不允许改名或删除。

内部结构：一个 `account` 一个 `MemorySession`（连接 + 双池向量集合 + BM25 索引 +
待确认队列），加一把库目录级的 single-writer 锁（A40）。所有会影响一致性的动作
（写入、确认、索引同步）都在锁内执行。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from hippocampus.core.locks import DEFAULT_TIMEOUT_S, WriterBusy, WriterLock
from hippocampus.core.types import (
    ConfirmResult,
    Dropped,
    Injection,
    MemoryItem,
    Scope,
    SearchResult,
    TurnResult,
    WriteResult,
)
from hippocampus.memory import account as account_mod
from hippocampus.memory import config as mem_config
from hippocampus.memory import database as db
from hippocampus.memory import dedup, pipeline, runtime
from hippocampus.memory import memory_bridge as mb
from hippocampus.memory import retrieval as rt

__all__ = ["MemoryCore", "MemoryCoreError", "WriterBusy"]

# D4（A35）：pending 确认块默认 TTL（7 天）。超时未裁决 → 标记"未决冲突"，旧值保持生效。
PENDING_TTL_MS = 7 * 24 * 3600 * 1000


class MemoryCoreError(RuntimeError):
    """记忆核心的显式错误（scope 非法 / 库不可用等）。"""


def _as_scope(scope: Scope | None) -> Scope:
    if scope is None:
        raise MemoryCoreError("MemoryCore 接口要求显式传 scope（v1 冻结约定）")
    if not isinstance(scope, Scope):
        raise MemoryCoreError(f"scope 必须是 Scope 实例，收到 {type(scope).__name__}")
    account_mod.safe_account_id(scope.account)  # 目录穿越校验
    return scope


def _dir_writable(path: Path) -> bool:
    """目录可写性探测（B5：chroma 不可写时 doctor 要能报出来）。

    用真实写探针而不是 os.access：Windows 上 os.access 对目录的 W_OK
    语义不可靠（只读属性不影响建文件），试写一发最接近 chroma 实际行为。
    """
    try:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        probe = path / f".wprobe_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


class MemoryCore:
    """记忆核心：写入 / 检索 / 注入装配 / 确认 / 生命周期 / 可审计读取。"""

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        scope: Scope | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        vector: bool = True,
        backend: Any = None,
    ) -> None:
        """home：数据根（None = 环境变量 HIPPOCAMPUS_HOME / ~/.hippocampus）。
        scope：默认作用域（每个方法仍要求显式 scope，这里只决定预热的库）。
        vector：False 时不建向量集合（纯词法降级；CI 与无 chromadb 环境用）。
        backend：存储后端（F5/A21）。None＝默认 `SqliteChromaBackend(vector=vector)`；
        传入自定义后端（实现 `core/backend.py` 的 `MemoryBackend` 协议）即可替换存储。
        """
        self.home = Path(home).expanduser().resolve() if home else runtime.default_data_root()
        self.timeout_s = float(timeout_s)
        self.vector = bool(vector)
        from hippocampus.core import backend as backend_mod

        self.backend: Any = backend if backend is not None else backend_mod.SqliteChromaBackend(vector=self.vector)
        self._sessions: dict[str, mb.MemorySession] = {}
        self._locks: dict[str, WriterLock] = {}
        self._registry_guard = threading.RLock()
        if scope is not None:
            self._session(_as_scope(scope))

    # ------------------------------------------------------------------
    # 内部：库与锁
    # ------------------------------------------------------------------

    def _data_dir(self, account: str) -> Path:
        return self.home / "accounts" / account_mod.safe_account_id(account)

    def _lock(self, account: str) -> WriterLock:
        with self._registry_guard:
            lock = self._locks.get(account)
            if lock is None:
                lock = WriterLock(self._data_dir(account), timeout_s=self.timeout_s)
                self._locks[account] = lock
            return lock

    def _session(self, scope: Scope) -> mb.MemorySession:
        account = scope.account
        with self._registry_guard:
            session = self._sessions.get(account)
            if session is None:
                data_dir = self._data_dir(account)
                # F5：会话由后端打开（默认 SqliteChromaBackend；换后端不改这里）
                with runtime.using_data_root(self.home):
                    session = self.backend.open_session(account, data_dir)
                self._sessions[account] = session
                self._restore_pending(session)
            return session

    def _restore_pending(self, session: mb.MemorySession) -> None:
        """把库里未决的确认块恢复进会话队列（跨进程的"挂起 → 下个会话裁决"）。

        前身只在内存里排队，进程退出即丢；这里从 `pending_blocks` 表重建块
        （记忆内容按 id 现取；条目已不存在的直接跳过）。
        D4（A35）：超过 TTL 的块**不恢复**进队列（视为已退休），由 `pending()`／
        `pending_blocks()` 里的 `_expire_pending` 在写锁内落库标记"未决冲突"。
        """
        from hippocampus.memory import confirm as confirm_mod

        try:
            db.ensure_pending_schema(session.conn)
            cutoff = db.now_ms() - PENDING_TTL_MS
            rows = session.conn.execute(
                "SELECT id, entries, conflicts FROM pending_blocks "
                "WHERE resolved_at IS NULL AND created_at >= ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        except Exception as e:
            sys.stderr.write(f"[core] 待确认恢复跳过（软失败）: {e}\n")
            return
        for row in rows:
            try:
                entries_raw = json.loads(row["entries"])
                conflicts = [tuple(c) for c in json.loads(row["conflicts"] or "[]")]
            except (TypeError, ValueError):
                continue
            entries = []
            for entry in entries_raw:
                mem = self._load_memory(session, entry.get("memory_id"))
                if mem is None:
                    continue
                entries.append({"num": entry.get("num"), "mem": mem, "is_new": bool(entry.get("is_new"))})
            if not entries:
                continue
            block = confirm_mod.ConfirmBlock(entries, conflicts)
            block.block_id = row["id"]
            session.pending_blocks.append(block)
            session.pending_block = block
        if len(session.pending_blocks) > 3:
            session.pending_blocks = session.pending_blocks[-3:]
            session.pending_block = session.pending_blocks[-1]

    # ------------------------------------------------------------------
    # v1 冻结接口
    # ------------------------------------------------------------------

    def write(
        self,
        scope: Scope,
        content: str,
        *,
        kind: str = "fact",
        source_quote: str = "",
        entities: list[str] | None = None,
        source: str | None = None,
        episode_id: str | None = None,
        explicit: bool = False,
    ) -> WriteResult:
        """写入一条记忆（**不经模型抽取**，用于显式写入与工具结果固化）。

        - `kind`：preference／fact／resource／status。
        - `source`：覆盖 scope.source（user／model／tool）。`model` 轨写入 `shadow=1`（永不注入）。
        - `explicit`：**调用方已表达确定意图**（用户直接编辑、或显式"记住 X"）→ 跳过
          "冲突挂起确认"这一步，直接写入；冲突挂起是给**对话里冒出来的**新说法用的
          （那时才需要人确认"你是不是改主意了"）。
        - 安全：内容过五组守卫；PII（E 组）直接丢弃并记入 `skipped`。
        - 去重：完全重复 → 跳过；语义命中 → 按 P1 分流（supersede／放行／挂起确认），**绝不静默丢弃**。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        src = (source or scope.source or "user").lower()
        shadow = 1 if src == "model" else 0
        out = WriteResult()

        text = (content or "").strip()
        if not text:
            out.skipped.append(Dropped(id="", reason="空内容", stage="validate"))
            return out

        with self._lock(scope.account).held(), session.lock:
            # 1) 安全守卫：E 组（PII）丢弃；C 组（密钥）标记入库（可审计）
            flag = 0
            try:
                from hippocampus.memory import security as secmod

                hits = secmod.check_text(text) + secmod.check_text(source_quote)
                if any(h.get("group") == "E" for h in hits):
                    out.skipped.append(Dropped(id="", reason="PII（E 组）丢弃", stage="security"))
                    return out
                flag = secmod.max_flag(hits)
            except Exception as e:  # 软失败：守卫坏掉不能阻断写入
                sys.stderr.write(f"[core] 安全守卫跳过（软失败）: {e}\n")

            # 2) 实体解析/创建
            entity_ids: list[str] = []
            entity_names = entities or []
            if not entity_names:
                try:
                    entity_names = mb._extract_explicit_entities(session.conn, text)
                except Exception:
                    entity_names = []
            for name in entity_names:
                name = (name or "").strip()
                if not name:
                    continue
                existing = db.find_entity_by_name(session.conn, name)
                if existing:
                    entity_ids.append(existing["id"])
                    out.entities[name] = existing["id"]
                else:
                    eid = db.add_entity(session.conn, name, "Abstract")
                    entity_ids.append(eid)
                    out.entities[name] = eid

            # 3) 去重 + P1 分流（语义命中不再 drop）＋ 规则冲突检测（无模型也能挂起确认）
            pid = episode_id or db.add_episode(
                session.conn, f"session_{scope.session}", "user" if src == "user" else "tool", text,
                entity_ids=entity_ids, security_flag=flag,
            )
            out.episode_id = pid

            exact = dedup.find_exact_duplicate(session.conn, kind, text, exclude_session_id=scope.session)
            if exact is not None:
                out.skipped.append(Dropped(id=exact, reason="完全重复（规范化后相等）", stage="dedup"))
                session.conn.commit()
                return out

            supersede_old: str | None = None
            hold_old: str | None = None
            hold_reason = "疑似重复但无法机械确认"
            if kind == "preference" or src != "model":
                sem = self._semantic_dup(session, kind, text, scope.session)
                if sem is not None:
                    old_row = session.conn.execute("SELECT content FROM memories WHERE id=?", (sem,)).fetchone()
                    action = dedup.classify_dup_action(old_row["content"] if old_row else "", text)
                    if action == "supersede":
                        supersede_old = sem
                    elif action == "admit":
                        pass
                    elif not explicit:
                        # P1 修复：机械判据拿不准时**不丢**——新条以 candidate 入库并挂起确认，
                        # 旧值在人工裁决前保持生效（A29 / A24）。
                        # explicit=True（用户直接编辑）不在此列：用户的编辑本身就是决定。
                        hold_old = sem
                        hold_reason = "语义疑似重复但判据不确定"
                        out.note = "语义疑似重复但判据不确定：已挂起待确认（不丢弃）"

            # 规则冲突检测（离线可用）：同对象取值不同 → 挂起确认，人工裁决前旧值继续生效。
            # 前身的冲突判定是纯 LLM（无 key 就断链），这条机械判据让"改口→确认"在离线档
            # 也成立（A36／A30-③／A24）。
            if hold_old is None and supersede_old is None and src != "model" and not explicit:
                try:
                    from hippocampus.memory import conflict as conflict_mod

                    for old_id, _placeholder, reason in conflict_mod.detect_rule_conflicts(
                        session.conn, text, kind, also_types=("preference", "fact")
                    ):
                        if old_id == exact:
                            continue
                        hold_old = old_id
                        hold_reason = reason
                        out.note = "与既有记忆冲突：已挂起待确认（旧值继续生效，不丢弃新值）"
                        break
                except Exception as e:
                    sys.stderr.write(f"[core] 规则冲突检测跳过（软失败）: {e}\n")

            status = "candidate" if hold_old else "active"
            mid = db.add_memory(
                session.conn,
                kind,
                text,
                entity_ids=entity_ids,
                source_quote=source_quote,
                source_episode_id=pid,
                security_flag=flag,
                shadow=shadow,
                status=status,
            )
            out.ids.append(mid)
            out.created = 1
            if supersede_old is not None:
                db.supersede_memory(session.conn, mid, supersede_old, source_episode_id=pid)
                out.superseded.append(supersede_old)
            if hold_old is not None:
                out.pending.append(mid)
                self._queue_pending(session, [(hold_old, mid, hold_reason)], reason=hold_reason)
            session.conn.commit()
            self._reindex(session)
            return out

    def _semantic_dup(self, session: mb.MemorySession, kind: str, text: str, session_id: str) -> str | None:
        if kind != "preference":
            return None
        collections = session.collections if session.collections.get("mem") is not None else None
        try:
            return dedup.find_semantic_duplicate(session.conn, collections, kind, text, exclude_session_id=session_id)
        except Exception:
            return None

    def _queue_pending(
        self, session: mb.MemorySession, conflicts: list[tuple[str, str, str]], *, reason: str = ""
    ) -> None:
        """挂起一个确认块：**入队 + 落库**（跨进程可继续裁决）。"""
        from hippocampus.memory import confirm as confirm_mod

        try:
            block = confirm_mod.build_confirm_block(session.conn, [c[1] for c in conflicts], conflicts)
            if block is None:
                return
            block.block_id = "pb_" + uuid.uuid4().hex[:12]
            session.push_pending_block(block)
            db.ensure_pending_schema(session.conn)
            session.conn.execute(
                "INSERT OR REPLACE INTO pending_blocks (id, entries, conflicts, reason, created_at) VALUES (?,?,?,?,?)",
                (
                    block.block_id,
                    json.dumps(
                        [
                            {"num": e["num"], "memory_id": e["mem"]["id"], "is_new": bool(e["is_new"])}
                            for e in block.entries
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps([list(c) for c in conflicts], ensure_ascii=False),
                    reason,
                    db.now_ms(),
                ),
            )
            session.conn.commit()
        except Exception as e:
            sys.stderr.write(f"[core] 挂起确认建块失败（软失败）: {e}\n")

    def _reindex(self, session: mb.MemorySession) -> None:
        """写后收尾：同步向量索引 + 重建词法索引。

        **故意不在每次写后清 WAL**：`purge_embeddings_wal` 会把 chroma 的
        embeddings_queue 里的行进删掉，而刚 upsert 的行可能还没落进索引——实测这样做
        会让"刚写的记忆检索不到"（demo 从 10/10 掉到 7/10）。WAL 的排空留在会话初始化
        时做（前身的位置），那里面对的是**别的进程**写下的积压。

        B5：同步失败不再只留一行 stderr——把原因记在会话的 `_index_error` 上，
        后续 `inject_finalize` 会把它带进注入结果（告警而非静默空注入），
        `doctor` 的索引健康行也会读到它。
        """
        try:
            rt.sync_index(session.conn, session.collections)
            session.idx = rt.build_bm25(session.conn)
            session._index_error = ""
        except Exception as e:
            session._index_error = f"索引同步失败: {e}"
            sys.stderr.write(f"[core] 索引同步失败（软失败）: {e}\n")

    def search(
        self,
        scope: Scope,
        query: str,
        *,
        limit: int = 8,
        flow: str = "user",
        include_dropped: bool = True,
    ) -> SearchResult:
        """四通道检索（语义／BM25／图／事件线索），返回条目与**被剔除候选及理由**。

        `include_dropped=False` 时只回条目（快路径）。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        result = SearchResult(flow=flow)
        query = (query or "").strip()
        if not query:
            return result
        with session.lock:
            raw = rt.retrieve(
                session.conn,
                query,
                session.collections,
                session.idx,
                top_k=limit,
                session_id=f"session_{scope.session}",
                flow=flow,
            )
        items, dropped = [], []
        for row in raw.get("results", []):
            row = dict(row)
            if row.get("kind") != "memory":
                continue
            mem = self._load_memory(session, row["doc_id"])
            if mem is None:
                continue
            reason = self._exclusion_reason(mem)
            if reason:
                dropped.append(Dropped(id=mem["id"], reason=reason, score=float(row.get("score") or 0.0), stage="filter"))
                continue
            items.append(
                self._to_item(
                    session, mem, score=float(row.get("score") or 0.0), channel=str(row.get("channel") or "")
                )
            )
        result.items = items[:limit] if limit else items
        result.channels = dict(raw.get("channels") or {})
        if include_dropped:
            result.dropped = dropped
        return result

    def inject_finalize(
        self,
        scope: Scope,
        query: str,
        *,
        flow: str = "user",
        seen_text: str = "",
        limit: int | None = None,
    ) -> Injection:
        """装配最终注入文本（stable／fluid 两层）。

        `seen_text`：本轮**模型已经看到的内容全文**（调用方自己给的一整段文本，不是
        传输层概念）。命中的记忆若已出现在其中，则不重复注入（防"重复注入已见内容"）。
        `limit`：覆盖注入条数上限（默认读活跃参数）。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        injection = Injection(flow=flow)
        query = (query or "").strip()
        if not query:
            return injection
        with session.lock:
            params = rt.get_active_params(session.conn)
            if not params.get("injection_enabled", True):
                injection.enabled = False
                return injection
            try:
                stable_text, fluid_text, filtered = mb.prepare_injection(
                    session, query, flow=flow, body_text=seen_text
                )
            except Exception as e:
                sys.stderr.write(f"[core] 注入装配失败（软失败，返回空注入）: {e}\n")
                return injection
        injection.stable_text = stable_text
        injection.fluid_text = fluid_text
        # [HIPPO] 确定性排序（A22 复演一致的前提）：分数相同时按 doc_id 定序。
        # 前身只按"分数 + 新鲜度"排，遇到同分时次序取决于向量库返回次序
        # （跨进程/跨库副本不稳定），复演会漂。
        filtered = sorted(
            filtered,
            key=lambda r: (-float(r.get("score") or 0.0), str(r.get("doc_id") or r.get("id") or "")),
        )
        if limit:
            filtered = filtered[:limit]
        for row in filtered:
            mem = self._load_memory(session, row.get("doc_id") or row.get("id"))
            if mem is None:
                continue
            injection.items.append(
                self._to_item(
                    session, mem, score=float(row.get("score") or 0.0), channel=str(row.get("channel") or "")
                )
            )
            injection.injected_ids.append(mem["id"])
        # B5：索引同步/检索异常时明确告警（不再静默空注入）——调用方（代理/Agent 形态）
        # 把 note 拼进响应即可让用户看到"记忆可能不完整"，而不是以为没记忆。
        index_err = getattr(session, "_index_error", "")
        if index_err:
            injection.note = f"（索引告警：{index_err}——本轮记忆注入可能不完整）"
        # A18（D1）：旁路审计通道——top-N=50 候选全集，供 explain 回答"那条为什么没进"
        # （旧版 explain 只看到检索 top-k 的被剔候选）。旁路：失败只记 stderr。
        self._audit_retrieval(session, scope, query, injection, flow)
        return injection

    def _audit_retrieval(
        self, session: mb.MemorySession, scope: Scope, query: str, injection: Injection, flow: str
    ) -> None:
        """旁路审计（AuditSink，A18/D1）：对同一 query 做一次 top-N=50 检索并落盘。

        与正常注入链（top-k≈8）分离：正常链决定"注入了什么"，审计链记录
        "本来还有哪些候选、为什么没进"。开关读活跃快照 `audit_enabled`。
        """
        AUDIT_TOP_N = 50  # noqa: N806 - A18 口径：候选全集上限
        try:
            from hippocampus.memory import audit

            params = rt.get_active_params(session.conn)
            if not params.get("audit_enabled", True):
                return
            raw = rt.retrieve(
                session.conn,
                query,
                session.collections,
                session.idx,
                top_k=AUDIT_TOP_N,
                session_id=f"session_{scope.session}",
                flow=flow,
            )
            injected_set = set(injection.injected_ids)
            candidates = []
            for row in raw.get("results", []):
                d = dict(row)
                doc_id = str(d.get("doc_id") or "")
                reason = ""
                if d.get("kind") == "memory":
                    mem = self._load_memory(session, doc_id)
                    if mem is None:
                        reason = "记忆不存在"
                    else:
                        reason = self._exclusion_reason(mem) or ""
                candidates.append(
                    {
                        "doc_id": doc_id,
                        "kind": d.get("kind"),
                        "channel": str(d.get("channel") or ""),
                        "score": round(float(d.get("score") or 0.0), 4),
                        "injected": doc_id in injected_set,
                        "dropped": bool(reason),
                        "reason": reason,
                    }
                )
            audit.record_retrieval(scope.account, query=query, candidates=candidates, injected_ids=injection.injected_ids)
        except Exception as e:
            sys.stderr.write(f"[core] 旁路审计失败（不中断）: {e}\n")

    def consolidate(
        self,
        scope: Scope,
        *,
        user_text: str = "",
        assistant_text: str = "",
    ) -> TurnResult:
        """一轮对话结束后的固化（抽取 → 消歧 → 去重 → 冲突 → 挂起确认）。

        两条轨：`user_text` 走 A 轨（preference／fact，可进正式记忆）；
        `assistant_text` 走 B 轨（status／resource，`shadow=1`，**永不注入**）。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        turn = TurnResult()
        with self._lock(scope.account).held(), session.lock:
            if user_text:
                summary = pipeline.process_user_message(
                    session.conn,
                    f"session_{scope.session}",
                    user_text,
                    memory_types=["preference", "fact"],
                    collections=session.collections,
                )
                turn.write.ids.extend(summary.get("memory_ids", []))
                turn.write.created = len(summary.get("memory_ids", []))
                turn.write.episode_id = summary.get("episode_id", "")
                turn.write.entities = summary.get("entity_map", {})
                block_text = mb.fire_track_a(session, user_text)
                if block_text:
                    turn.confirm_block = block_text
                # [HIPPO] 规则冲突检测：对话里冒出来的改口也要能挂起确认。
                # 前身的对话冲突判定是纯 LLM（无 key 就断链），而"改口 → 挂起"恰恰是
                # 招牌机制——所以这里补一条机械判据，离线档同样成立（A36／A30-③／A24）。
                rule_block = self._rule_conflicts_after_write(session, summary.get("memory_ids", []))
                if rule_block:
                    turn.confirm_block = (turn.confirm_block + rule_block).strip()
            if assistant_text:
                block_text = mb.after_response(session, user_text or "（无用户轮）", [])
                if block_text:
                    turn.confirm_block = (turn.confirm_block + block_text).strip()
            # 轮末守卫（前身口径，B1/B2/B3）：漏抽补实体 + 实体消歧。
            # 放在最后一次 reindex 之前，让索引收尾带上守卫的改动。
            self._run_turn_guards(scope, session, user_text)
            self._reindex(session)
            turn.pending = len(session.pending_blocks)
        return turn

    def _run_turn_guards(self, scope: Scope, session: mb.MemorySession, user_text: str) -> None:
        """轮末守卫：漏抽检测（孤立经历补实体 + 语义命中但实体未命中补实体）＋实体消歧第二层。

        - `missed_extract.scan_and_fix`：孤立经历（entity_ids 为空）补实体，≤3 次；
        - `retrieval_guard.run_retrieval_guard`：语义通道命中但实体未命中的经历补实体（只加不删）；
        - `disambiguate.run_disambiguation`：实体两两消歧合并（无模型端点时软失败跳过）。

        全部软失败：守卫坏掉只记 stderr，**绝不阻断本轮固化结果**（A28 的"漏抽守卫"口径）。
        """
        if not user_text or not user_text.strip():
            return
        try:
            raw = rt.retrieve(
                session.conn,
                user_text,
                session.collections,
                session.idx,
                top_k=8,
                session_id=f"session_{scope.session}",
                flow="user",
            )
            channels = raw.get("channels") or {}
            from hippocampus.memory import disambiguate, missed_extract, retrieval_guard

            retrieval_guard.run_retrieval_guard(
                session.conn, channels.get("entity_ids") or [], channels, verbose=False
            )
            missed_extract.scan_and_fix(session.conn, verbose=False, max_episodes=20)
            disambiguate.run_disambiguation(session.conn, max_pairs=5)
            session.conn.commit()
        except Exception as e:
            sys.stderr.write(f"[core] 轮末守卫失败（软失败，不阻断固化）: {e}\n")

    def _rule_conflicts_after_write(self, session: mb.MemorySession, new_ids: list[str]) -> str:
        """对话写入之后做一次规则冲突检测：命中就把新条挂起并返回确认块文本。

        处置与 `write()` 里的一致：**新条降为 candidate（不丢），旧值在裁决前继续生效**。
        """
        from hippocampus.memory import conflict as conflict_mod

        blocks: list[str] = []
        for mid in new_ids:
            mem = self._load_memory(session, mid)
            if mem is None or mem.get("status") != "active":
                continue
            try:
                conflicts = conflict_mod.detect_rule_conflicts(
                    session.conn,
                    mem.get("content") or "",
                    mem.get("type"),
                    also_types=("preference", "fact"),
                )
            except Exception as e:
                sys.stderr.write(f"[core] 对话规则冲突检测跳过（软失败）: {e}\n")
                continue
            conflicts = [c for c in conflicts if c[0] != mid]
            if not conflicts:
                continue
            session.conn.execute(
                "UPDATE memories SET status='candidate', updated_at=? WHERE id=?", (db.now_ms(), mid)
            )
            session.conn.commit()
            self._queue_pending(session, [(conflicts[0][0], mid, conflicts[0][2])], reason=conflicts[0][2])
            if session.pending_blocks:
                blocks.append(session.pending_blocks[-1].render())
        return "\n\n---\n" + "\n".join(blocks) if blocks else ""

    def confirm(self, scope: Scope, text: str) -> ConfirmResult | None:
        """消费一条确认指令（`确认 n` / `否决`）。非确认指令返回 None。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        with self._lock(scope.account).held(), session.lock:
            matched = session.match_confirmation(text or "")
            if matched is None:
                return None
            block, decision = matched
            from hippocampus.memory import confirm as confirm_mod

            output = confirm_mod.apply_confirmation(session.conn, block, decision)
            session.pop_pending_block(block)
            block_id = getattr(block, "block_id", "")
            if block_id:
                try:
                    session.conn.execute("UPDATE pending_blocks SET resolved_at=? WHERE id=?", (db.now_ms(), block_id))
                    session.conn.commit()
                except Exception as e:
                    sys.stderr.write(f"[core] 确认落库标记跳过（软失败）: {e}\n")
            kind = decision[0]
            winner_id = (decision[1] or "") if kind == "confirm" else ""
            if winner_id:
                # 候选转正：确认胜出后必须从 candidate 变回 active，否则它仍然不参与注入
                # （candidate 的定义就是"等裁决"；裁决完了就该生效）。
                session.conn.execute(
                    "UPDATE memories SET status='active', updated_at=? WHERE id=? AND status='candidate'",
                    (db.now_ms(), winner_id),
                )
                session.conn.commit()
            self._reindex(session)
            return ConfirmResult(
                decision="confirm" if kind == "confirm" else "veto",
                text=output,
                winner_id=winner_id,
                loser_ids=[e["mem"]["id"] for e in block.entries],
                block_text=block.render(),
            )

    # ------------------------------------------------------------------
    # 记忆 CRUD（A9，supersede 语义）
    # ------------------------------------------------------------------

    def list_memories(
        self,
        scope: Scope,
        *,
        limit: int = 20,
        kind: str | None = None,
        status: str | None = "active",
        include_shadow: bool = False,
        order: str = "recent",
    ) -> list[MemoryItem]:
        """列出记忆（默认只看 active、不看模型观察轨）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        where, params = ["1=1"], []
        if status:
            where.append("status=?")
            params.append(status)
        if kind:
            where.append("type=?")
            params.append(kind)
        if not include_shadow:
            where.append("COALESCE(shadow,0)=0")
        order_sql = "created_at DESC" if order == "recent" else "last_hit_at DESC, created_at DESC"
        rows = session.conn.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT ?",  # noqa: S608
            (*params, int(limit)),
        ).fetchall()
        return [self._to_item(session, dict(r)) for r in rows]

    def get_memory(self, scope: Scope, memory_id: str) -> MemoryItem | None:
        """按 id 取一条记忆（含 superseded/归档，便于追溯）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        mem = self._load_memory(session, memory_id)
        return self._to_item(session, mem) if mem else None

    def update_memory(
        self,
        scope: Scope,
        memory_id: str,
        *,
        content: str = "",
        reason: str = "",
    ) -> WriteResult:
        """修改 = **supersede 语义**：写新条并取代旧条（旧条保留可追溯，不删除）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        old = self._load_memory(session, memory_id)
        if old is None:
            raise MemoryCoreError(f"记忆不存在: {memory_id}")
        new_text = (content or "").strip()
        if not new_text:
            raise MemoryCoreError("update_memory 需要 content")
        result = self.write(
            scope,
            new_text,
            kind=old["type"],
            source_quote=(reason or f"由 {memory_id} 修改而来"),
            episode_id=old.get("source_episode_id") or None,
            explicit=True,  # 用户直接编辑 = 已表达确定意图，不需要再挂起确认
        )
        if result.ids:
            with self._lock(scope.account).held(), session.lock:
                db.supersede_memory(session.conn, result.ids[0], memory_id)
                session.conn.commit()
                self._reindex(session)
            result.superseded.append(memory_id)
        return result

    def delete_memory(self, scope: Scope, memory_id: str, *, reason: str = "") -> bool:
        """删除 = **软删**（archived，不物理删除）：记忆可审计是项目的底层立场。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        with self._lock(scope.account).held(), session.lock:
            row = session.conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return False
            session.conn.execute(
                "UPDATE memories SET status='archived', lifecycle='archived', change_context=?, updated_at=? "
                "WHERE id=?",
                (reason or "用户删除", db.now_ms(), memory_id),
            )
            session.conn.commit()
            self._reindex(session)
        return True

    # ------------------------------------------------------------------
    # 状态与开关
    # ------------------------------------------------------------------

    def pending(self, scope: Scope) -> list[dict[str, Any]]:
        """未决确认队列（块 → 编号 → 记忆）。先清一遍 TTL 过期块（旧值保持生效）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        out: list[dict[str, Any]] = []
        with self._lock(scope.account).held(), session.lock:
            self._expire_pending(session)
            for block in list(session.pending_blocks):
                for entry in block.entries:
                    out.append(
                        {
                            "num": entry["num"],
                            "id": entry["mem"]["id"],
                            "kind": entry["mem"].get("type", ""),
                            "content": entry["mem"].get("content", ""),
                            "is_new": entry["is_new"],
                        }
                    )
        return out

    def pending_blocks(self, scope: Scope) -> list[dict[str, Any]]:
        """未决确认块（块级视图，含 TTL 剩余；`memory review --pending` 用）。

        返回每块的 block_id／created_at／TTL 剩余（ms）／条目列表。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        with self._lock(scope.account).held(), session.lock:
            self._expire_pending(session)
            now = db.now_ms()
            out: list[dict[str, Any]] = []
            for block in list(session.pending_blocks):
                block_id = getattr(block, "block_id", "")
                created_at = 0
                if block_id:
                    row = session.conn.execute(
                        "SELECT created_at FROM pending_blocks WHERE id=?", (block_id,)
                    ).fetchone()
                    if row:
                        created_at = int(row["created_at"] or 0)
                ttl_remaining = max(0, PENDING_TTL_MS - (now - created_at))
                out.append(
                    {
                        "block_id": block_id,
                        "created_at": created_at,
                        "ttl_ms": PENDING_TTL_MS,
                        "ttl_remaining_ms": ttl_remaining,
                        "entries": [
                            {
                                "num": e["num"],
                                "id": e["mem"]["id"],
                                "kind": e["mem"].get("type", ""),
                                "content": e["mem"].get("content", ""),
                                "is_new": e["is_new"],
                            }
                            for e in block.entries
                        ],
                    }
                )
            return out

    def _expire_pending(self, session: mb.MemorySession, ttl_ms: int = PENDING_TTL_MS) -> list[str]:
        """TTL 过期处置（D4/A35）：未决确认块超时未裁决 → 标记已决＋记录"未决冲突"。

        - **旧值保持生效**：旧记忆本来就是 active（候选不注入），无需改动；
        - 新候选保持 candidate（不注入、不消失，可追溯）；
        - 块级 `reason` 追加"TTL 未决冲突"，`resolved_at` 落库——之后不再出现在
          pending 队列，也不会被误恢复。
        返回被标记的块 id 列表。软失败不阻断。
        """
        try:
            db.ensure_pending_schema(session.conn)
            cutoff = db.now_ms() - int(ttl_ms)
            rows = session.conn.execute(
                "SELECT id FROM pending_blocks WHERE resolved_at IS NULL AND created_at < ?", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            session.conn.execute(
                "UPDATE pending_blocks SET resolved_at=?, reason=reason || '（TTL 未决冲突："
                "超时未裁决，旧值保持生效）' WHERE resolved_at IS NULL AND created_at < ?",
                (db.now_ms(), cutoff),
            )
            # 从内存队列移除过期块（进程内长时间运行也会过期）
            if session.pending_blocks:
                expired = set(ids)
                session.pending_blocks = [
                    b for b in session.pending_blocks if getattr(b, "block_id", "") not in expired
                ]
                session.pending_block = session.pending_blocks[-1] if session.pending_blocks else None
            session.conn.commit()
            return ids
        except Exception as e:
            sys.stderr.write(f"[core] pending TTL 清理失败（软失败）: {e}\n")
            return []

    def suspicious(self, scope: Scope) -> list[dict[str, Any]]:
        """可疑项审计（`memory review --suspicious`，A34/D3）。

        三类可疑：① 带安全标记的记忆（security_flag>0）；② 长期未决的候选
        （含 TTL 未决冲突）；③ 生命周期异常（非 active/dormant/archived）。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        with self._lock(scope.account).held(), session.lock:
            self._expire_pending(session)
            out: list[dict[str, Any]] = []
            rows = session.conn.execute(
                "SELECT id, type, content, status, lifecycle, security_flag, created_at "
                "FROM memories WHERE security_flag > 0 OR status='candidate' "
                "OR lifecycle NOT IN ('active','dormant','archived') ORDER BY created_at LIMIT 200"
            ).fetchall()
            for r in rows:
                flags = []
                if (r["security_flag"] or 0) > 0:
                    flags.append(f"安全标记 {r['security_flag']}")
                if r["status"] == "candidate":
                    flags.append("待确认候选（未生效）")
                if r["lifecycle"] not in ("active", "dormant", "archived"):
                    flags.append(f"生命周期 {r['lifecycle']}")
                out.append(
                    {
                        "id": r["id"],
                        "kind": r["type"],
                        "content": r["content"],
                        "status": r["status"],
                        "lifecycle": r["lifecycle"],
                        "flags": flags,
                    }
                )
            # TTL 未决冲突的块（已退休但从未裁决）
            blocks = session.conn.execute(
                "SELECT id, reason, created_at, resolved_at FROM pending_blocks "
                "WHERE resolved_at IS NOT NULL AND reason LIKE '%TTL 未决冲突%' ORDER BY resolved_at DESC LIMIT 50"
            ).fetchall()
            for b in blocks:
                out.append(
                    {
                        "id": b["id"],
                        "kind": "pending-block",
                        "content": f"TTL 未决冲突（创建 {b['created_at']}，标记 {b['resolved_at']}）",
                        "status": "ttl-unresolved",
                        "lifecycle": "-",
                        "flags": ["TTL 未决冲突：旧值保持生效"],
                    }
                )
            return out

    def stats(self, scope: Scope) -> dict[str, int]:
        """库内计数（实体/记忆/经历/关系/待确认/候选）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        counts = db.count_stats(session.conn)
        counts["pending"] = len(session.pending_blocks)
        counts["candidates"] = session.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='candidate'"
        ).fetchone()[0]
        return counts

    def set_switch(self, scope: Scope, command: str) -> str | None:
        """开关口令（`关闭记忆`／`打开记忆`／`停止学习`／`继续学习`）。"""
        scope = _as_scope(scope)
        session = self._session(scope)
        with self._lock(scope.account).held(), session.lock:
            return mb.handle_switch_command(session.conn, command or "")

    def lock_status(self, scope: Scope) -> dict[str, Any]:
        """库目录锁状态（doctor 用，A40）。"""
        scope = _as_scope(scope)
        lock = self._lock(scope.account)
        return {
            "path": str(lock.path),
            "held_by_self": lock.held_by_self,
            "held_by_other": lock.is_held_by_other(),
            "stale": lock.is_stale(),
            "meta": lock.read_info(),
        }

    def unlock(self, scope: Scope, *, stale_after_s: float = 300.0) -> bool:
        """回收僵尸锁（`hippocampus doctor --unlock`）。"""
        scope = _as_scope(scope)
        return self._lock(scope.account).force_release(stale_after_s=stale_after_s)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _load_memory(self, session: mb.MemorySession, memory_id: str | None) -> dict[str, Any] | None:
        if not memory_id:
            return None
        row = session.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def _exclusion_reason(self, mem: dict[str, Any]) -> str:
        """注入前过滤的剔除理由（A18 解释的数据源）。"""
        if (mem.get("security_flag") or 0) > 0:
            return f"安全标记 security_flag={mem['security_flag']}"
        if mem.get("status") == "superseded":
            return "已被更新的记忆取代（superseded）"
        if mem.get("status") == "candidate":
            return "待确认候选（尚未生效）"
        if mem.get("status") not in ("active", None):
            return f"状态 {mem.get('status')} 不参与注入"
        if (mem.get("shadow") or 0) == 1:
            return "模型观察轨（shadow=1），永不注入"
        if mem.get("lifecycle") in ("dormant", "archived"):
            return f"生命周期 {mem['lifecycle']}（已降级/归档）"
        return ""

    def _to_item(
        self,
        session: mb.MemorySession,
        mem: dict[str, Any],
        *,
        score: float = 0.0,
        channel: str = "",
    ) -> MemoryItem:
        import json

        def _ids(raw: Any) -> list[str]:
            try:
                return list(json.loads(raw or "[]"))
            except (TypeError, ValueError):
                return []

        supersedes: list[str] = []
        try:
            rows = session.conn.execute(
                "SELECT to_id FROM relations WHERE from_type='memory' AND from_id=? AND rel_type='SUPERSEDES'",
                (mem.get("id"),),
            ).fetchall()
            supersedes = [r[0] for r in rows]
        except Exception:
            supersedes = []
        return MemoryItem(
            id=mem.get("id", ""),
            kind=mem.get("type", ""),
            content=mem.get("content", ""),
            status=mem.get("status", "active"),
            lifecycle=mem.get("lifecycle", "active"),
            shadow=int(mem.get("shadow") or 0),
            score=score,
            channel=channel,
            source_quote=mem.get("source_quote", ""),
            entities=_ids(mem.get("entity_ids")),
            created_at=int(mem.get("created_at") or 0),
            last_hit_at=int(mem.get("last_hit_at") or 0),
            supersedes=supersedes,
        )

    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭所有会话连接（不删数据）。

        用 `MemorySession.close()`（conn＋chroma client 一起关）而不是只关 sqlite：
        只关 conn 会让 chroma PersistentClient 的句柄积累到 GC，全量测试跑几百个
        core 实例后触发 "Too many open files"（实测 test_t6 真实服务器测试被拖垮）。
        """
        with self._registry_guard:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception:
                    pass
            self._sessions.clear()
            self._locks.clear()

    def __enter__(self) -> MemoryCore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def embedding_tier(self) -> dict[str, Any]:
        """当前嵌入档（doctor 用，A37）。"""
        cfg = mem_config.get_embedding_config()
        model = cfg["model"]
        if model == "builtin-hash":
            tier, needs_download = "内置档（零下载）", False
        elif model.startswith("onnx:"):
            tier, needs_download = "ONNX 本地档", False
        else:
            tier, needs_download = "chromadb 内置档", True
        return {"model": model, "tier": tier, "needs_download": needs_download, "enabled": cfg["enabled"]}

    def index_health(self, scope: Scope) -> dict[str, Any]:
        """索引健康（doctor 用，B5）：chroma 可写性＋集合条数 vs 库内 active 条数＋同步错误。

        词法档（未建向量集合）返回 `collection_count=None`，健康结论记"不适用"——
        词法档的 BM25 索引随写重建，不存在 chroma 那套一致性风险。
        """
        scope = _as_scope(scope)
        session = self._session(scope)
        active = session.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND COALESCE(shadow,0)=0"
        ).fetchone()[0]
        mem_col = (session.collections or {}).get("mem")
        if mem_col is None:
            col_count: Any = None
        else:
            try:
                col_count = int(mem_col.count())
            except Exception as e:
                col_count = f"读取失败: {e}"
        writable = True
        if mem_col is not None:
            writable = _dir_writable(session.chroma_dir)
        try:
            queue = int(rt.embeddings_queue_depth(session.chroma_dir))
        except Exception:
            queue = -1
        last_error = str(getattr(session, "_index_error", "") or "")
        if mem_col is None:
            healthy: Any = None  # 不适用（词法档）
        else:
            healthy = bool(writable and isinstance(col_count, int) and not last_error)
        return {
            "active_memories": active,
            "collection_count": col_count,
            "chroma_dir": str(session.chroma_dir),
            "chroma_writable": writable,
            "embeddings_queue": queue,
            "last_error": last_error,
            "healthy": healthy,
        }
