"""JSONL 追加写 + **大小轮转**（D1：audit／observe 只增不轮转的治理）。

三条日志（`audit.jsonl`／`observe.jsonl`／`trajectory`）都是"只追加"的旁路文件。长期运行
只增不轮转 → 磁盘无界增长。这里给一个共用的追加器：

- 追加前估算"加上这条是否会超过上限"，超了就**滚动**：`x.jsonl` → `x.jsonl.1` → `x.jsonl.2` …
  超过保留份数的**最旧一份直接丢掉**（日志是有损可接受的旁路数据；上限与份数都写在配置里）；
- 追加本身仍是原地 append（不重写文件），轮转只在到达上限那一次发生；
- **软失败**：任何异常只记 stderr，绝不阻断注入／固化主流程（与三条日志原本的纪律一致）。

上限与份数来自配置 `observability.jsonl_max_bytes` / `observability.jsonl_keep`
（可用环境变量 `HIPPOCAMPUS_LOG_MAX_BYTES` / `HIPPOCAMPUS_LOG_KEEP` 覆盖）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 8 * 1024 * 1024  # 单文件上限 8 MiB
DEFAULT_KEEP = 3  # 保留的滚动份数（不含当前文件）
MIN_MAX_BYTES = 64 * 1024
MAX_KEEP = 20


def _as_int(value: Any, fallback: int, *, low: int, high: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, out))


def log_limits() -> tuple[int, int]:
    """读轮转上限与保留份数：(max_bytes, keep)。配置 → 环境变量 → 默认。"""
    try:
        from hippocampus.memory import config as cfg

        conf = (cfg.get_config().get("observability") or {})
    except Exception:
        conf = {}
    max_bytes = _as_int(os.environ.get("HIPPOCAMPUS_LOG_MAX_BYTES") or conf.get("jsonl_max_bytes"),
                        DEFAULT_MAX_BYTES, low=MIN_MAX_BYTES, high=1 << 40)
    keep = _as_int(os.environ.get("HIPPOCAMPUS_LOG_KEEP") or conf.get("jsonl_keep"),
                   DEFAULT_KEEP, low=0, high=MAX_KEEP)
    return max_bytes, keep


def rotate_if_needed(path: Path, *, max_bytes: int | None = None, keep: int | None = None) -> list[str]:
    """超上限就滚动文件，返回被滚动/丢弃的文件名列表（供测试与诊断）。

    滚动规则：`x.jsonl.(n-1)` → `x.jsonl.n`（从旧到新），最旧的超过 `keep` 份就删掉。
    """
    max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else int(max_bytes)
    keep = DEFAULT_KEEP if keep is None else int(keep)
    actions: list[str] = []
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return actions
        oldest = path.with_name(path.name + f".{keep}")
        if keep <= 0:
            # 不保留份数：直接清空当前文件（截断，不动 inode，别的写者不会拿到悬空句柄）
            path.write_text("", encoding="utf-8")
            return [f"{path.name}:truncated"]
        if oldest.exists():
            oldest.unlink()
            actions.append(f"{oldest.name}:dropped")
        for idx in range(keep - 1, 0, -1):
            src = path.with_name(path.name + f".{idx}")
            if src.exists():
                src.replace(path.with_name(path.name + f".{idx + 1}"))
                actions.append(f"{src.name}->{path.name}.{idx + 1}")
        path.replace(path.with_name(path.name + ".1"))
        actions.append(f"{path.name}->{path.name}.1")
    except Exception as e:  # 软失败：日志出问题不该影响主流程
        sys.stderr.write(f"[log] 轮转失败（不中断，继续追加）: {e}\n")
    return actions


def append_jsonl(path: str | Path, event: dict[str, Any], *, max_bytes: int | None = None, keep: int | None = None) -> None:
    """把一条事件按 JSONL 追加到 path（先按需轮转）。**软失败**，不抛。"""
    target = Path(path)
    try:
        if max_bytes is None or keep is None:
            cfg_max, cfg_keep = log_limits()
            max_bytes = cfg_max if max_bytes is None else max_bytes
            keep = cfg_keep if keep is None else keep
        line = json.dumps(event, ensure_ascii=False) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_needed(target, max_bytes=max_bytes, keep=keep)
        with open(target, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        sys.stderr.write(f"[log] 写入失败（不中断）: {e}\n")


def load_events(path: str | Path | None, *, include_rotated: bool = False) -> list[dict[str, Any]]:
    """读 JSONL（坏行跳过）。`include_rotated=True` 时按"旧→新"合并滚动份再读当前文件。"""
    if path is None:
        return []
    target = Path(path)
    files: list[Path] = []
    if include_rotated:
        rotated = sorted(
            (p for p in target.parent.glob(target.name + ".*") if p.name[len(target.name) + 1 :].isdigit()),
            key=lambda p: int(p.name[len(target.name) + 1 :]),
            reverse=True,
        )
        files.extend(rotated)
    if target.exists():
        files.append(target)
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
        except OSError:
            continue
    return out


__all__ = ["DEFAULT_KEEP", "DEFAULT_MAX_BYTES", "append_jsonl", "load_events", "log_limits", "rotate_if_needed"]
