"""把前身代理的**格式转换层**抽入本包（抽取式 vendoring，与记忆层同一套规矩）。

前身的代理支持**三种入站格式**（OpenAI Chat / OpenAI Responses / Anthropic Messages）
× **三种上游格式**（按 `api_mode`），转换矩阵在 `format_converters.py`（1303 行）+
`proxy_app.py` 里的两个 responses↔chat 辅助函数。本项目第一版只实现了 chat 一条，
这里把转换层补回来。

做法与 `vendor_memory_modules.py` 一致：**只改写 import，实现原样**。
跑法：
    python scripts/vendor_proxy_modules.py --src D:/AI/HERMES/hippocampus_prototype
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 前身模块 → 目标包内文件名
VENDORED = {
    "format_converters.py": "format_converters.py",
    "llm_proxy.py": "llm_proxy.py",
    "responses_adapter.py": "responses_adapter.py",
}

_INTERNAL = {"format_converters", "llm_proxy", "responses_adapter"}

_IMPORT_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kw>import|from)[ \t]+(?P<mod>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?P<rest>(?:[ \t]+as[ \t]+[A-Za-z_][A-Za-z_0-9]*)?)(?P<tail>.*)$"
)
_MAIN_GUARD_RE = re.compile(r'^if __name__ == "__main__":', re.MULTILINE)


def _rewrite(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        m = _IMPORT_RE.match(line.rstrip("\r\n"))
        if m and m.group("mod") in _INTERNAL:
            indent, kw, mod, rest, tail = (m.group(k) for k in ("indent", "kw", "mod", "rest", "tail"))
            if kw == "import":
                out.append(f"{indent}from hippocampus.proxy import {mod}{rest}{tail}\n")
            else:
                out.append(f"{indent}from hippocampus.proxy.{mod}{rest}{tail}\n")
            continue
        out.append(line)
    return "".join(out)


def _strip_main(text: str) -> str:
    m = _MAIN_GUARD_RE.search(text)
    return (text[: m.start()].rstrip() + "\n") if m else text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--dst", default=Path(__file__).resolve().parents[1] / "src" / "hippocampus" / "proxy", type=Path)
    args = ap.parse_args()

    args.dst.mkdir(parents=True, exist_ok=True)
    changed, unchanged, missing = [], [], []
    for src_name, dst_name in VENDORED.items():
        src = args.src / src_name
        if not src.exists():
            missing.append(src_name)
            continue
        text = _rewrite(_strip_main(src.read_text(encoding="utf-8")))
        header = (
            f"# 由前身 hippocampus_prototype/{src_name} 抽取移植（vendoring），仅做包内 import 改写。\n"
            "# 上游实现细节保持原样；本项目的改动点集中标注 [HIPPO]。\n\n"
        )
        target = args.dst / dst_name
        body = header + text
        if target.exists() and target.read_text(encoding="utf-8") == body:
            unchanged.append(dst_name)
            continue
        target.write_text(body, encoding="utf-8", newline="\n")
        changed.append(dst_name)

    print(f"vendored: {len(VENDORED)} | 变更 {len(changed)} | 未变 {len(unchanged)} | 缺源 {len(missing)}")
    if changed:
        print("  变更:", " ".join(changed))
    if missing:
        print("  缺源:", " ".join(missing))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
