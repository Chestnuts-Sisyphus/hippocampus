"""把前身（hippocampus_prototype）的记忆层模块抽入本包（抽取式 vendoring）。

设计：前身是**扁平模块 + 裸 import**（`import database as db`），本包是**包结构**。
本脚本做机械改写（裸 import → 包内绝对 import），并剥掉 `if __name__ == "__main__"`
自检块（它们会往当前目录写库，属开发脚手架；断言能力由随迁的 tests/ 承担）。

改写是**离线幂等**的：源路径不变则每次产出相同结果。跑法：
    python scripts/vendor_memory_modules.py --src D:/AI/HERMES/hippocampus_prototype
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 前身模块 → 目标包内模块名（同名前身模块直接映射）
VENDORED = [
    "database",
    "retrieval",
    "memory_bridge",
    "extract",
    "pipeline",
    "dedup",
    "conflict",
    "confirm",
    "lifecycle",
    "offline_consolidation",
    "schema_compact",
    "security",
    "retrieval_guard",
    "hub_guard",
    "missed_extract",
    "observe_log",
    "event_time",
    "disambiguate",
    "feedback",
    "observe",
    "diagnose",
    "embedding_models",
]

# 前身内部模块名（裸 import 的目标）→ 本包内引用
_INTERNAL = set(VENDORED) | {"runtime", "account", "config", "llm"}

_IMPORT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kw>import|from)[ \t]+(?P<mod>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?P<rest>(?:[ \t]+as[ \t]+[A-Za-z_][A-Za-z_0-9]*)?)(?P<tail>.*)$"
)

_MAIN_GUARD_RE = re.compile(r'^if __name__ == "__main__":', re.MULTILINE)


def _rewrite_lines(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _IMPORT_RE.match(line.rstrip("\r\n"))
        if not m:
            out.append(line)
            continue
        mod = m.group("mod")
        if mod not in _INTERNAL:
            out.append(line)
            continue
        indent, kw, rest, tail = m.group("indent"), m.group("kw"), m.group("rest"), m.group("tail")
        if kw == "import":
            new = f"{indent}from hippocampus.memory import {mod}{rest}{tail}"
        else:
            # from X import a, b  →  from hippocampus.memory.X import a, b（tail 含 " import a, b"）
            new = f"{indent}from hippocampus.memory.{mod}{rest}{tail}"
        out.append(new + "\n")
    return "".join(out)


def _strip_main_block(text: str) -> str:
    """剥掉文件末尾的 `if __name__ == "__main__":` 块（到文件尾）。"""
    m = _MAIN_GUARD_RE.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip() + "\n"


def _header(src_name: str) -> str:
    return (
        f"# 由前身 hippocampus_prototype/{src_name}.py 抽取移植（vendoring），"
        "仅做包内 import 改写与全局依赖解耦。\n"
        "# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。\n\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path, help="前身工程根（只读）")
    ap.add_argument("--dst", default=Path(__file__).resolve().parents[1] / "src" / "hippocampus" / "memory", type=Path)
    ap.add_argument("--check", action="store_true", help="只校验不改写：产出与现存文件不一致则退出码 1")
    args = ap.parse_args()

    dst: Path = args.dst
    dst.mkdir(parents=True, exist_ok=True)
    changed, unchanged, missing = [], [], []
    for mod in VENDORED:
        src_file = args.src / f"{mod}.py"
        if not src_file.exists():
            missing.append(mod)
            continue
        text = src_file.read_text(encoding="utf-8")
        text = _strip_main_block(text)
        text = _rewrite_lines(text)
        text = _header(mod) + text
        target = dst / f"{mod}.py"
        if target.exists() and target.read_text(encoding="utf-8") == text:
            unchanged.append(mod)
            continue
        changed.append(mod)
        if not args.check:
            target.write_text(text, encoding="utf-8", newline="\n")

    print(f"vendored: {len(VENDORED)} 模块 | 变更 {len(changed)} | 未变 {len(unchanged)} | 缺源 {len(missing)}")
    if changed:
        print("  变更:", " ".join(changed))
    if missing:
        print("  缺源:", " ".join(missing))
    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
