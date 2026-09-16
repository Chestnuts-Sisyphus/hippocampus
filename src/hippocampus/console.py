"""控制台输出编码兜底。

为什么需要：Windows 上 Python 的 stdout 默认走本地代码页（CI 里是 **cp1252**），
打印 `✓` 或中文会直接抛 `UnicodeEncodeError` —— 一个"打印"问题能把整条命令打挂
（实测：GitHub Actions 的 windows-latest 上 `scripts/check_interface.py` 因此 exit 1，
而 Linux 全绿）。

处置：把 stdout/stderr 重挂成 UTF-8 且 `errors="replace"` ——
**宁可显示成问号，也不要因为一个字符崩掉整条命令**。
"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    """把标准输出/错误切到 UTF-8（不支持 reconfigure 的环境静默跳过）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


__all__ = ["force_utf8"]
