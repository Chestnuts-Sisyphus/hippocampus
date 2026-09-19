"""验收入口：公开面泄露闸（八轮 V3）——公开仓库里不得出现「定位线索」形态的文本。

为什么要有这个脚本：本仓 `visibility=PUBLIC`。七轮深夜两处泄露全靠人肉 `git grep` 才发现
（文档里的凭据登记簿路径＋行号；`tests/` 里硬编码的本机个人目录路径），
且第一批脱敏**漏扫了 `tests/`**——所以本闸的扫描范围必须含 `*.py`，只扫文档会重演同一个错。

判据（命中任一形态即退出非零）：
1. 凭据登记簿目录与其文件名（路径与「第几行」都是泄露面，凭据值本身另有 `scan_credentials.py` 管）；
2. 仓库外正本目录（知识库 vault、当前事实源目录）；
3. 本机账号目录形态（Windows 盘符用户目录／POSIX 主目录）与本机账号名；
4. 「第 <数字> 行」这类行号指针——单独出现无害，混进文档就意味着在指向某个仓外正本的偏移。

三条实现口径：
- **本文件自身不含任何完整泄露串**：待查串一律由片段运行时拼装（见 `_frag`），
  因此闸可以把 `scripts/scan_public_leak.py` 与守护测试自己也纳入扫描范围，不做任何豁免。
- **命中只报形态名与文件行号，不回显命中文本**：CI 日志是公开面，把命中的串打印出来等于二次泄露。
- **只扫 tracked 文件**（`git ls-files`），与 `git grep` 口径一致：未跟踪的本机草稿不属公开面。

跑法：`python scripts/scan_public_leak.py`
退出码 0 = 干净。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 扫描范围：文档＋配置＋CI＋**源码与测试**（*.py 是本轮新增的必查项）
SCAN_SUFFIXES = {
    ".py", ".md", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".json", ".txt", ".example", ".sh", ".ps1", ".env",
}
# git ls-files 不可用时的兜底遍历：跳过这些目录
SKIP_DIRS = {".venv", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", "build", "dist", ".mypy_cache", "node_modules"}


def _frag(*parts: str) -> str:
    """把待查串按片段拼装，片段之间允许正斜杠／反斜杠／连字符／下划线／空白。

    这样源码里不出现任何完整泄露串，本脚本与守护测试本身可被自己的规则扫描（零豁免）。
    """
    if len(parts) == 1:
        return re.escape(parts[0])
    # 分隔符**可选**：账号名这类串拼出来时片段之间没有分隔符（漏检过一次）
    glue = r"(?:\s*[/\\\-\u2013\u2014_]\s*)?"
    return glue.join(re.escape(p) for p in parts)


LEAK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_frag("D:", "AI", "KEY")), "凭据母库目录路径"),
    (re.compile(_frag("GITHUB", "TOKENS")), "凭据登记簿文件名"),
    (re.compile(r"第\s*\d+\s*行"), "行号指针（仓外正本偏移）"),
    (re.compile(_frag("D:", "Obsidian")), "知识库 vault 路径"),
    (re.compile(_frag("求职", "天津")), "当前事实源目录名"),
    (re.compile(_frag("C:", "Users") + r"[/\\]+[^\s\"'`/\\]+"), "本机用户目录绝对路径"),
    (re.compile(r"[/\\]home[/\\][a-z][a-z0-9_\-]{2,}"), "POSIX 用户主目录路径"),
    (re.compile(r"[/\\]Users[/\\][a-z0-9_.\-]{3,}", re.IGNORECASE), "macOS 用户主目录路径"),
    (re.compile(_frag("Admini", "strator"), re.IGNORECASE), "本机账号名"),
]



# force-utf8 shim：Windows 控制台默认代码页（CI 里是 cp1252）无法编码 ✓ 等字符，会让"打印"把命令打挂。
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def iter_tracked_files(root: Path = ROOT) -> list[Path]:
    """公开面 = tracked 树。git 不可用时退回目录遍历（口径相同：只看在版本控制里的文本文件）。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=60, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _walk(root)
    files: list[Path] = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace").replace("\\", "/")
        path = root / rel
        if path.suffix.lower() in SCAN_SUFFIXES and path.is_file():
            files.append(path)
    return files


def _walk(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            out.append(path)
    return out


def scan_file(path: Path) -> list[dict]:
    """单文件扫描。返回 [{file, line, rule}]——**故意不含命中文本**。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings: list[dict] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for pattern, label in LEAK_RULES:
            if pattern.search(line):
                findings.append({"file": path.as_posix(), "line": idx, "rule": label})
    return findings


def scan_files(paths: list[Path]) -> list[dict]:
    findings: list[dict] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings


def main() -> int:
    files = iter_tracked_files()
    py_files = [p for p in files if p.suffix.lower() == ".py"]
    findings = scan_files(files)
    print(f"公开面泄露闸：扫描 tracked 文本 {len(files)} 个（其中 *.py {len(py_files)} 个）")
    if findings:
        for row in findings:
            print(f"  ✗ {row['file']}:{row['line']} 命中「{row['rule']}」（命中文本不回显）")
        print("\n公开面扫描：有命中（必须脱敏或改由环境变量注入正本路径）")
        return 1
    print("✓ 公开面扫描：零命中（无凭据定位线索、无仓外正本路径、无本机账号痕迹）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
