"""X15（十轮）：pyproject 声明与 extras 的轻量对账闸（正则版教训后改 AST）。

十轮批 A 残桩补全记录：本文件前会话用正则扫 import，把中文注释词（"包内内部模块"）
误当模块名、手写 stdlib 清单必漏 → 结构上不可靠。改用 AST 解析；
双向完整口径（可选探测/转管理白名单）在 `test_n52_dependency_gate.py`，两闸并存互补。
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
SRC_DIR = REPO / "src" / "hippocampus"

# try-import 的可选探测通道（未装则降级，口径见 docs/dependency-audit.md §三）
OPTIONAL_PROBES = {"keyring"}


def test_pyproject_deps_match_imports():
    """验证 pyproject.toml 中的依赖覆盖了 src 顶层 import 的全部第三方模块。"""
    import re
    import tomllib

    def pkg_of(spec: str) -> str:
        return re.split(r"[<>=!~;\[ ]", spec.strip())[0].strip().lower()

    data = tomllib.loads(PYPROJECT.read_bytes().decode("utf-8"))
    deps: set[str] = set()
    for spec in data["project"].get("dependencies", []):
        deps.add(pkg_of(spec))
    for items in (data["project"].get("optional-dependencies") or {}).values():
        deps.update(pkg_of(spec) for spec in items)

    imports_found: set[str] = set()
    for py_file in SRC_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports_found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports_found.add(node.module.split(".")[0])

    # PEP 503 归一（分发名 langgraph-checkpoint 与 import 名 langgraph_checkpoint 同形对照）
    normalized = {d.replace("-", "_") for d in deps}
    third_party = imports_found - set(sys.stdlib_module_names) - {"hippocampus"} - OPTIONAL_PROBES
    missing = {m for m in third_party if m not in normalized and m.replace("_", "-") not in {d for d in deps}}
    assert not missing, f"以下导入在 pyproject.toml 中未声明：{sorted(missing)}"


def test_all_extras_defined():
    """验证所有可选依赖都正确定义（用 tomllib 读真实 section，不用 regex 扫全文）。"""
    import tomllib

    data = tomllib.loads(PYPROJECT.read_bytes().decode("utf-8"))
    extras = set((data["project"].get("optional-dependencies") or {}).keys())

    # 验证至少定义了主要 extra
    for name in ("vector", "onnx", "proxy", "agent", "eval", "all", "dev"):
        assert name in extras, f"应定义 {name} extra"
