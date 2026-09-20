"""十轮 X15：pyproject 声明 ↔ 代码实际 import 的**一致性闸**。

钉两个方向，防止依赖漂移再发生（本轮实测缺陷：numpy/onnxruntime/tokenizers 在 src
被显式 import 却从未声明，靠 chromadb 传递依赖"偶遇"）：

1. **正向（不得缺声明）**：`src/` 实际 import 的每个第三方模块，必须能在
   pyproject 的 dependencies ∪ optional-dependencies 里找到；
   例外只有 `OPTIONAL_PROBES`（try-import 的可选探测通道，文档已登记）。
2. **反向（不得留陈旧声明）**：core/extras 里声明的每个包，必须被 src/scripts/tests
   之一 import；例外只有 `TRANSITIVE_ALLOW`（传递依赖/伴生包，逐条带理由）。

跑法：`pytest tests/test_n52_dependency_gate.py`（纯静态解析，零出站、不装任何包）。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# src 里 try-import 的可选探测通道（未安装 → 静默降级，见 docs/dependency-audit.md §三）
OPTIONAL_PROBES = {"keyring"}

# 声明了但不被本仓代码直接 import 的传递依赖/伴生包（必须逐条写理由）
TRANSITIVE_ALLOW = {
    "langchain-core": "langgraph 的传递依赖，agent 形态工具协议（as_langchain）的伴生包",
    "langchain-openai": "agent 形态 OpenAI 兼容端点由 langgraph 运行期装配，不经本仓直接 import",
}

STDLIB_NOTE = "sys.stdlib_module_names 判标准库"


def _normalize(dist_name: str) -> str:
    """PEP 503 式归一：小写 + `-`→`_`，使 `langchain-core` 与 import 名 `langchain_core` 对得上。"""
    return dist_name.lower().replace("-", "_").split(";")[0].strip().split(" ")[0]


def _package_names(spec: str) -> str:
    """从 `numpy>=1.26` 这类 specifier 提取包名。"""
    for sep in ("<", ">", "=", "!", "~", "["):
        spec = spec.split(sep)[0]
    return _normalize(spec.strip())


def _declared_packages() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_bytes().decode("utf-8"))
    project = data["project"]
    specs: list[str] = list(project.get("dependencies", []))
    for group, items in (project.get("optional-dependencies") or {}).items():
        if group == "dev":  # dev 组是工具链，不参与 src 一致性判定
            continue
        specs.extend(items)
    return {_package_names(s) for s in specs}


def _imported_top_levels(*dirs: Path) -> set[str]:
    out: set[str] = set()
    for d in dirs:
        for path in sorted(d.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        out.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    out.add(node.module.split(".")[0])
    return out


def _third_party(imports: set[str]) -> set[str]:
    import sys

    local = {"hippocampus", "conftest"}
    return {m for m in imports if m not in sys.stdlib_module_names and m not in local and not m.startswith("_")}


def test_src_imports_are_declared():
    """正向闸：src 的每个第三方 import 都有 pyproject 声明（或登记为可选探测）。"""
    src_imports = _third_party(_imported_top_levels(ROOT / "src"))
    declared = _declared_packages()
    missing = src_imports - declared - OPTIONAL_PROBES
    assert not missing, f"src 实际 import 但 pyproject 未声明：{sorted(missing)}（新增依赖须同步声明，见 docs/dependency-audit.md §三）"


def test_declared_packages_are_imported():
    """反向闸：pyproject 声明的包必须真被代码 import（或带理由地进传递白名单）。"""
    code_imports = _third_party(_imported_top_levels(ROOT / "src", ROOT / "scripts", ROOT / "tests"))
    declared = _declared_packages()
    allow = {_normalize(k) for k in TRANSITIVE_ALLOW}
    stale = {p for p in declared - code_imports if p not in allow}
    assert not stale, f"pyproject 声明但无人 import 且无转管理由：{sorted(stale)}（陈旧声明要销账或补理由）"


def test_optional_probe_keyring_is_try_import():
    """keyring 只允许以 try-import 形态存在——升级为硬依赖就违反'无则只用环境变量'口径。"""
    files = list((ROOT / "src").rglob("*.py"))
    users = [p for p in files if "import keyring" in p.read_text(encoding="utf-8")]
    assert users, "预期 config.py 仍有 keyring 可选探测"
    for p in users:
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "import keyring" in line:
                window = "\n".join(lines[max(0, i - 4) : i + 1])
                assert "try:" in window, f"{p.relative_to(ROOT)}:{i + 1} 的 keyring import 不在 try 块内"
