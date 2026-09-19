"""九轮 W9（K14）：时间预算台账 ↔ 真实等待点的完整性闸——**缺行即红**。

为什么要这台闸：八轮 V13 立了台账，但台账是**手写清单**——新加一个 `sleep(5)` 或把兜底值改小，
没人会想起去补一行。台账一旦不全，后面读它的人就会把"表里没有"当成"这里没问题"，
比没有表更危险。所以把表反过来对代码校验：代码里每个会在时间上阻塞的调用点，
都必须在 `docs/ci-time-budgets.md` 里有一行同时写出**这个文件**和**这个值**。

三条口径：
- 扫描范围＝`tests/` 与 `scripts/` 的 `*.py`（`src/` 不在此闸范围：产品代码的等待值是运行时配置，
  另有 URL 校验与超时护栏，不属"测试假红"这一类）；
- 值可以是字面量，也可以是同文件内的具名常量——常量必须能在本文件里解析出数值，
  否则判红（**改名把值藏起来**正是这类清单最容易失守的地方）；
- 台账行的判据＝同一行内既出现该文件名、又出现该数值（数值按浮点比较，`300` 与 `300.0` 同值）。

对照测试：植一条台账里没有的等待点（新文件／新值）必须被判红——台账"只写不改"的假绿就是这么来的。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "docs" / "ci-time-budgets.md"
SCAN_DIRS = ("tests", "scripts")

# 什么算「等待点」：任何会在时间上阻塞的调用点。宁滥勿缺——漏一类，闸就有一个洞。
WAIT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("轮询间隔", re.compile(r"time\.sleep\(([^)]*)\)")),
    ("线程收尾", re.compile(r"\b\w*(?:thread|th|t)\.join\(([^)]*)\)")),
    ("事件/进程等待", re.compile(r"\.wait\(([^)]*)\)")),
    ("请求与子进程超时", re.compile(r"\btimeout=([^,)\n:]+)")),
    ("死线算式", re.compile(r"time\.time\(\)\s*\+\s*([^)\n]+)")),
]

NUMERIC = re.compile(r"^\s*([\d.]+)\s*$")
CONST_DEF = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*([\d.]+)", re.M)
# 台账行里的等待值都写成秒；只认这些单位后缀出现在同表
FILE_REF = re.compile(r"[\w./\\-]+\.py")


def _code_lines(text: str) -> list[str]:
    """把字符串字面量与注释抹成空白，只留代码本体。

    不这么做会有两类假点：本闸 docstring 里的示例写法、以及植桩测试写在字符串里的
    `time.sleep(4.5)`——它们都不阻塞任何线程，却要台账给行，等于让表去迁就散文。
    """
    out = text.splitlines()
    string_types = {tokenize.STRING, tokenize.COMMENT}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        token_type = getattr(tokenize, name, None)
        if token_type is not None:
            string_types.add(token_type)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return out  # 解析不了就整行扫：宁可多报，不可漏报
    for token in tokens:
        if token.type not in string_types:
            continue
        (srow, scol), (erow, ecol) = token.start, token.end
        if srow == erow:
            line = out[srow - 1]
            # 只抹这个 token 自己的跨度：抹到行尾会把同行后半段的 `timeout=` 一起吞掉（假阴性＝闸有洞）
            out[srow - 1] = line[:scol] + " " * max(ecol - scol, 1) + line[ecol:]
            continue
        out[srow - 1] = out[srow - 1][:scol]
        for row in range(srow + 1, erow):
            out[row - 1] = ""
        out[erow - 1] = " " * ecol + out[erow - 1][ecol:]
    return out


def _const_map(text: str) -> dict[str, float]:
    """同文件内的大写常量 → 数值（等待值常被提成常量，见 scripts/demo_flow.py）。"""
    return {name: float(value) for name, value in CONST_DEF.findall(text)}


def _resolve(expr: str, consts: dict[str, float]) -> float | None:
    """把等待点的参数解析成秒。返回 None＝不是时间量（如 `timeout=None` 的形参、无参 `wait()`）。"""
    raw = expr.strip()
    if raw.startswith("timeout="):
        raw = raw[len("timeout=") :].strip()
    if not raw or raw == "None":
        return None
    m = NUMERIC.match(raw)
    if m:
        return float(m.group(1))
    if raw in consts:
        return consts[raw]
    # 形如 `args.settle`／外部常量这类解析不出的，交调用方按「写明原式」的更严口径登记
    return float("nan")


def scan_wait_points(root: Path = REPO) -> list[dict]:
    """扫出全部等待点：[{file, line, expr, value, kind}]（value=nan 表示解析不出秒数，须按原式登记）。"""
    points: list[dict] = []
    for subdir in SCAN_DIRS:
        for path in sorted((root / subdir).glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            consts = _const_map(text)
            for idx, line in enumerate(_code_lines(text), start=1):
                for kind, pattern in WAIT_PATTERNS:
                    for expr in pattern.findall(line):
                        value = _resolve(expr, consts)
                        if value is None:
                            continue
                        points.append(
                            {
                                "file": path.name,
                                "rel": path.relative_to(root).as_posix(),
                                "line": idx,
                                "expr": expr.strip(),
                                "value": value,
                                "kind": kind,
                            }
                        )
    return points


def ledger_lines(path: Path = LEDGER) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            out.add(float(raw))
        except ValueError:
            pass
    return out


def find_missing_rows(points: list[dict], lines: list[str]) -> list[str]:
    """台账 ↔ 等待点：返回缺行说明（空＝全覆盖）。"""
    named: list[tuple[set[str], set[float], str]] = []
    for line in lines:
        files = {Path(ref.replace("\\", "/")).name for ref in FILE_REF.findall(line)}
        named.append((files, _numbers(line), line))
    missing: list[str] = []
    seen: set[tuple[str, float, str]] = set()
    for point in points:
        value = point["value"]
        if value != value:  # NaN：静态解析不出秒数（运行时参数／外部常量）→ 按原式登记
            key = (point["file"], value, point["expr"])
            if key in seen:
                continue
            if not any(point["file"] in files and point["expr"] in line for files, _nums, line in named):
                missing.append(
                    f"{point['rel']}:{point['line']} 等待值 `{point['expr']}` 静态解析不出秒数，"
                    f"台账须有一行同时写明 `{point['file']}` 与该原式（说明它由谁决定）"
                )
                seen.add(key)
            continue
        key = (point["file"], value, "")
        if key in seen:
            continue
        covered = any(point["file"] in files and value in numbers for files, numbers, _line in named)
        if not covered:
            missing.append(
                f"{point['rel']}:{point['line']} {point['kind']}＝{point['expr']}"
                f"（{value:g} 秒）在台账里缺行：需一行同时写明 `{point['file']}` 与该数值"
            )
            seen.add(key)
    return missing


def test_ledger_file_exists() -> None:
    assert LEDGER.exists(), f"台账正本不存在：{LEDGER}"


def test_every_wait_point_has_a_ledger_row() -> None:
    """代码里的每个等待点都必须在台账里有一行（文件＋数值同时出现）。"""
    missing = find_missing_rows(scan_wait_points(), ledger_lines())
    assert not missing, "时间预算台账缺行（新等待点必须登记定性：功能性预算／死锁兜底／请求超时）：\n  " + "\n  ".join(
        missing
    )


def test_planted_wait_point_without_a_row_turns_the_gate_red(tmp_path: Path) -> None:
    """对照测试：植一个新等待点（台账里没有的值）→ 闸必须红。"""
    planted_dir = tmp_path / "tests"
    planted_dir.mkdir(parents=True)
    (planted_dir / "test_zz_planted_wait.py").write_text(
        "import time\n\n\ndef test_planted():\n    time.sleep(4.5)\n    assert True\n",
        encoding="utf-8",
    )
    points = scan_wait_points(tmp_path)
    assert [p for p in points if p["file"] == "test_zz_planted_wait.py"], "植的等待点没被扫到，闸形同虚设"
    missing = find_missing_rows(points, ledger_lines())
    assert any("test_zz_planted_wait.py" in row and "4.5" in row for row in missing), (
        "台账没有这一行，闸却判绿——说明它根本没在校验"
    )


def test_named_constant_wait_values_are_not_hidden(tmp_path: Path) -> None:
    """对照测试：等待值提成具名常量后，值仍要进台账；常量在别处定义（本文件解析不出）→ 判红。"""
    planted_dir = tmp_path / "scripts"
    planted_dir.mkdir(parents=True)
    (planted_dir / "planted_hidden.py").write_text(
        "from somewhere import FAR_AWAY_S\n\n\ndef go():\n    deadline = FAR_AWAY_S\n    time.sleep(FAR_AWAY_S)\n",
        encoding="utf-8",
    )
    missing = find_missing_rows(scan_wait_points(tmp_path), ledger_lines())
    assert any("FAR_AWAY_S" in row and "静态解析不出秒数" in row for row in missing), (
        "把等待值塞进外部常量就能躲过台账，闸没起作用"
    )


def test_ledger_keeps_the_three_categories_and_the_no_retry_discipline() -> None:
    """台账本身要留判定口径，否则后来者无法给新等待点归类。"""
    text = LEDGER.read_text(encoding="utf-8")
    for needle in ("功能性预算", "死锁兜底", "请求超时", "不加 retry"):
        assert needle in text, f"台账丢了口径：{needle}"
    # 九轮 W9 新登记的四处定性必须在表里，不能只写在代码注释里
    for needle in ("test_n28_r6_engineering.py", "demo_flow.py", "live_management_smoke.py", "test_n31_r7_serve.py"):
        assert needle in text, f"台账缺 {needle} 的定性行"
