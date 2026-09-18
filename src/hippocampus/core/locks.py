"""单写者保护（验收 A40）。

背景：两个形态（代理 / Agent）可能同时打开同一个记忆库。SQLite 自己的锁只保护
单条语句，保护不了"向量索引与主库一致"这种跨资源不变量。所以按**库目录**加一把
跨进程写锁：

- 拿锁 = 对 `<data_dir>/.writer.lock` 取得**操作系统级排他锁**（Windows `msvcrt` /
  POSIX `fcntl`）。进程崩溃时由 OS 自动释放 → 僵尸锁天然不会永久阻塞。
- 锁文件内容记 `pid` 与心跳时间戳，仅用于诊断与"看起来卡住了"时的强制回收。
- 拿不到锁：**默认排队等待**（`timeout_s` 内轮询），超时抛 `WriterBusy`；
  `--force-unlock` 走 `force_release()`（只在明显过期时才动，避免踩掉活进程的锁）。

实现不用 subprocess / 不用 shell，文件打开一律用 `Path` 与内建 `open`（只读模式）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".writer.lock"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_STALE_S = 300.0


class WriterBusy(RuntimeError):
    """写锁被其他进程持有且等待超时。"""


def _acquire_os_lock(handle) -> bool:
    """尝试取得 OS 级排他锁（非阻塞）。成功 True / 被占 False。"""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class WriterLock:
    """库目录级写锁（可重入：同实例同线程重复获取只加计数）。"""

    def __init__(self, data_dir: str | Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / LOCK_NAME
        self.timeout_s = float(timeout_s)
        self._handle = None
        self._depth = 0
        self._guard = threading.RLock()
        self.acquired_at = 0.0

    # ---------- 诊断 ----------

    def read_info(self) -> dict:
        """读锁文件元信息（无文件/损坏 → 空 dict）。"""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def is_held_by_other(self) -> bool:
        """是否有其他进程持有（探测：试拿一次即放）。"""
        if not self.path.exists():
            return False
        try:
            handle = self.path.open("a+b")
        except OSError:
            return False
        try:
            if _acquire_os_lock(handle):
                _release_os_lock(handle)
                return False
            return True
        finally:
            handle.close()

    def held_locally(self) -> bool:
        """本进程当前是否持有（重入计数 > 0）。LRU 逐出时用来判断锁对象能否删。"""
        with self._guard:
            return self._depth > 0

    def is_stale(self, *, stale_after_s: float = DEFAULT_STALE_S) -> bool:
        """锁文件存在但无人持锁、且心跳过期 → 视为僵尸锁。"""
        if not self.path.exists():
            return False
        if self.is_held_by_other():
            return False
        info = self.read_info()
        if not info:
            return True                     # 无元信息且无人持锁：残留文件
        ts = float(info.get("ts") or 0.0)
        if ts <= 0:                         # 元信息里没有有效时间戳 → 视为已过期
            return True
        return (time.time() - ts) > stale_after_s

    def force_release(self, *, stale_after_s: float = DEFAULT_STALE_S) -> bool:
        """强制回收僵尸锁（只回收真僵尸：无人持锁且心跳过期/元信息缺失）。"""
        if not self.is_stale(stale_after_s=stale_after_s):
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    # ---------- 获取/释放 ----------

    def acquire(self, *, wait: bool = True) -> None:
        with self._guard:
            if self._depth > 0:
                self._depth += 1
                return
            self.data_dir.mkdir(parents=True, exist_ok=True)
            deadline = time.time() + (self.timeout_s if wait else 0.0)
            handle = self.path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    if _acquire_os_lock(handle):
                        break
                    if not wait or time.time() >= deadline:
                        raise WriterBusy(
                            f"记忆库写锁被占用（{self.path}）；"
                            f"元信息={self.read_info()}；可等另一位写者结束或用 `hippocampus doctor --unlock` 回收僵尸锁"
                        ) from None
                    time.sleep(0.05)
                self._write_info(handle)
                self._handle = handle
                self._depth = 1
                self.acquired_at = time.time()
            except BaseException:
                if self._handle is None:
                    handle.close()
                raise

    def _write_info(self, handle) -> None:
        info = json.dumps({"pid": os.getpid(), "ts": time.time(), "host": os.environ.get("COMPUTERNAME", "")})
        try:
            handle.seek(1)
            handle.truncate()
            handle.write(info.encode("utf-8"))
            handle.flush()
        except OSError:
            pass

    def heartbeat(self) -> None:
        """刷新心跳（长任务期间调用，避免被误判为僵尸）。"""
        if self._handle is not None:
            self._write_info(self._handle)

    def release(self) -> None:
        with self._guard:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth > 0:
                return
            if self._handle is not None:
                _release_os_lock(self._handle)
                self._handle.close()
                self._handle = None
            try:
                self.path.unlink()
            except OSError:
                pass

    @contextmanager
    def held(self, *, wait: bool = True):
        self.acquire(wait=wait)
        try:
            yield self
        finally:
            self.release()

    @property
    def held_by_self(self) -> bool:
        return self._depth > 0
