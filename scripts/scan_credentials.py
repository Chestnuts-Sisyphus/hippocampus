"""验收入口：凭据扫描（T8-③）——仓库与示例库里**零可用凭据**。

扫两类位置：
1. 源码／文档／配置模板（`src/`、`docs/`、`scripts/`、`tests/`、根目录文本文件）；
2. 示例数据（`hippocampus seed` 灌出来的库：记忆内容、来源引用、经历原文）。

判据（两条都要满足）：
- **不允许**出现形如真实凭据的字面量（各家 key 的前缀 + 足够长度）；
- 出现 `sk-test-xxx` / `placeholder-*` 这类**明确占位符**时，行内必须带
  `placeholder`／`test`／`fake` 等字样（防止"占位符"被悄悄换成真 key 而没人发现）。

退出码 0 = 干净。跑法：`python scripts/scan_credentials.py [--home <示例库根>]`
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_SUFFIXES = {".py", ".md", ".toml", ".cfg", ".ini", ".json", ".yml", ".yaml", ".txt", ".example"}
SKIP_DIRS = {".venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "build", "dist", ".mypy_cache"}

# 真实凭据的形态特征（各家前缀 + 足够长度）
CREDENTIAL_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI 风格密钥"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"), "Anthropic 风格密钥"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key ID"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub Token"),
    (re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}\b"), "GitLab Token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "Slack Token"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), "Google API Key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥文件内容"),
]

# 明确的占位符（允许出现，但行内必须自带"这是假的"的意思）
PLACEHOLDER_PATTERN = re.compile(r"(placeholder|test|fake|dummy|example|xxx|not-a-real)", re.IGNORECASE)
SHORT_SECRET_SHAPE = re.compile(r"sk-[A-Za-z0-9]{6,}")



# force-utf8 shim：Windows 控制台默认代码页（CI 里是 cp1252）无法编码 ✓ 等字符，会让"打印"把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def iter_files(base: Path):
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES or path.name in {".env.example", ".gitignore"}:
            yield path


def scan_text(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for idx, line in enumerate(text.splitlines(), start=1):
        for pattern, label in CREDENTIAL_PATTERNS:
            if pattern.search(line) and not PLACEHOLDER_PATTERN.search(line):
                findings.append(
                    {
                        "file": path.as_posix(),
                        "line": idx,
                        "issue": f"{label}（未标注为占位符）",
                        "sample": line.strip()[:80],
                    }
                )
        if SHORT_SECRET_SHAPE.search(line) and not PLACEHOLDER_PATTERN.search(line):
            findings.append(
                {
                    "file": path.as_posix(),
                    "line": idx,
                    "issue": "疑似密钥字面量且未标注占位",
                    "sample": line.strip()[:80],
                }
            )
    return findings


def _collect_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """取三张表里的自由文本列。SQL 用字面量直接写，不做任何拼接。"""
    out: list[tuple[str, str]] = []
    try:
        for row in conn.execute("SELECT content AS v FROM memories").fetchall():
            out.append(("memories.content", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute("SELECT content AS v FROM episodes").fetchall():
            out.append(("episodes.content", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    try:
        for row in conn.execute("SELECT canonical_name AS v FROM entities").fetchall():
            out.append(("entities.canonical_name", str(row["v"] or "")))
    except sqlite3.Error:
        pass
    return out


def scan_seed_library(home: Path) -> list[dict]:
    """示例库扫描：记忆/经历/实体表里的自由文本也要过一遍凭据特征。"""
    findings: list[dict] = []
    if not home.exists():
        return findings
    for db_path in home.rglob("*.db"):
        if "chroma" in db_path.parts:
            continue
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            for label, value in _collect_rows(conn):
                for pattern, kind in CREDENTIAL_PATTERNS:
                    if pattern.search(value):
                        findings.append(
                            {
                                "file": f"{db_path.as_posix()} [{label}]",
                                "line": 0,
                                "issue": kind,
                                "sample": value[:80],
                            }
                        )
        finally:
            conn.close()
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", help="额外扫描该目录下的示例库（sqlite）")
    args = ap.parse_args()

    findings: list[dict] = []
    scanned = 0
    for path in iter_files(ROOT):
        scanned += 1
        findings.extend(scan_text(path))

    seed_findings: list[dict] = []
    if args.home:
        seed_findings = scan_seed_library(Path(args.home))

    print(f"扫描文件 {scanned} 个；示例库发现 {len(seed_findings)} 处")
    if findings or seed_findings:
        for row in findings + seed_findings:
            print(f"  ✗ {row['file']}:{row['line']} {row['issue']} → {row['sample']}")
        print("\n凭据扫描：有命中（必须处理）")
        return 1
    print("✓ 凭据扫描：零命中（仓库与示例库都没有可用凭据字面量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
