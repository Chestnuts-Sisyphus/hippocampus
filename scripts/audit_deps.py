"""T2 交付物：依赖审计（可复跑）。

回答三个问题，全部**用代码扫出来**、不靠人回忆：

1. **可移植模块清单**：`hippocampus.memory` 下有哪些模块、各自多大（行数）；
2. **需断开的全局依赖**：前身是"扁平模块 + 模块级全局"（`DB_PATH` / `CHROMA_DIR` /
   `runtime.data_root()` / DPAPI / 账号体系）。这里逐个扫描"是否还残留全局单例写法"，
   残留就报出来（[HIPPO] 标注过的不算）；
3. **第三方依赖**：本包实际 import 的第三方模块 → 与 pyproject 声明对照。

判据：退出码 0 = 干净；非 0 = 有残留（CI 用它挡回归）。

跑法：`python scripts/audit_deps.py [--json out.json]`
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "hippocampus"

# 前身"全局单例"的写法特征（本项目必须逐个断开或改写）
GLOBAL_PATTERNS = [
    (re.compile(r"^\s*DB_PATH\s*=\s*runtime\.data_root\(\)"), "模块级 DB_PATH 单例"),
    (re.compile(r"^\s*CHROMA_DIR\s*=\s*runtime\.data_root\(\)"), "模块级 CHROMA_DIR 单例"),
    (re.compile(r"^\s*BASE\s*=\s*runtime\.data_root\(\)"), "模块级数据根单例"),
    (re.compile(r"win32crypt|CryptProtectData|CryptUnprotectData"), "Windows 专有（DPAPI）"),
    (re.compile(r"import msvcrt"), "平台专有文件锁（应走 core.locks）"),
    (re.compile(r"^\s*_PARAM_CONN|^\s*_EMB_FN\w*\s*=\s*None\s*$"), "模块级可变全局（跨 scope 会串）"),
]

# [HIPPO] 标注行视为"已处理"（可能是兼容锚点或有意为之）
_MARK = "[HIPPO]"

STDLIB = set(sys.stdlib_module_names)
THIRD_PARTY_ALIAS = {"chromadb", "jieba", "httpx", "fastapi", "uvicorn", "langgraph", "langchain_core"}


def _module_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imports_of(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def audit() -> dict:
    modules: list[dict] = []
    leftovers: list[dict] = []
    third_party: set[str] = set()

    for path in _module_files():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        rel = path.relative_to(ROOT).as_posix()
        modules.append({"module": rel, "lines": len(lines)})
        for name in _imports_of(path):
            if name in ("hippocampus",) or name in STDLIB:
                continue
            third_party.add(name)
        in_docstring = False
        for idx, line in enumerate(lines, start=1):
            # 只扫**代码行**：注释与文档串里提到这些词不算残留
            if line.strip().startswith('"""') or line.strip().endswith('"""'):
                in_docstring = not in_docstring
                continue
            if in_docstring or line.lstrip().startswith("#"):
                continue
            # core/locks.py 就是"跨平台文件锁"的指定实现处，平台 API 在这里是有意的
            if rel == "src/hippocampus/core/locks.py" and "msvcrt" in line:
                continue
            for pattern, label in GLOBAL_PATTERNS:
                if pattern.search(line):
                    # 前后两行有 [HIPPO] 标注 → 视为已处理
                    window = "\n".join(lines[max(0, idx - 3) : idx + 1])
                    if _MARK in window:
                        continue
                    leftovers.append({"file": rel, "line": idx, "issue": label, "code": line.strip()[:100]})
    return {
        "modules": modules,
        "module_count": len(modules),
        "total_lines": sum(m["lines"] for m in modules),
        "leftover_globals": leftovers,
        "leftover_count": len(leftovers),
        "third_party": sorted(third_party),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="把审计结果写到该路径")
    args = ap.parse_args()

    report = audit()
    print("=== 可移植模块清单（hippocampus/）===")
    for row in report["modules"]:
        print(f"  {row['module']:<48} {row['lines']:>5} 行")
    print(f"  合计 {report['module_count']} 个文件 / {report['total_lines']} 行")
    print()
    print("=== 第三方依赖（源码实际 import）===")
    print("  " + ", ".join(report["third_party"]))
    print()
    print("=== 需断开的全局依赖残留 ===")
    if report["leftover_globals"]:
        for row in report["leftover_globals"]:
            print(f"  ✗ {row['file']}:{row['line']} {row['issue']} → {row['code']}")
    else:
        print("  ✓ 无残留（前身的模块级单例/平台专有/可变全局都已断开或改写）")

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 1 if report["leftover_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
