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
# 九轮 W5：git 对象（提交信息／标签注解／Release 正文）里的**历史既有命中条数**。
# 公开仓历史不改写（八轮 V2 已裁定），所以这里不要求归零，只要求"只减不增"：
# 实测出处为 `git:commit:2419926`（八轮 V7 补记那条 message 里的目录名与行号指针各 1 条）。
# 新写一条线索就会顶过基线 → 闸红。
GIT_TEXT_BASELINE = 2
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


def scan_findings(findings: list[dict], *, stage: str) -> int:
    """打印命中（只报出处与形态名，不回显文本）并给出退出码。"""
    if not findings:
        return 0
    for row in findings:
        print(f"  ✗ [{stage}] {row['file']}:{row['line']} 命中「{row['rule']}」（命中文本不回显）")
    return 1


def scan_lines(lines: list[tuple[str, int, str]]) -> list[dict]:
    findings: list[dict] = []
    for source, idx, line in lines:
        for pattern, label in LEAK_RULES:
            if pattern.search(line):
                findings.append({"file": source, "line": idx, "rule": label})
    return findings


def _git_text_lines(root: Path) -> list[tuple[str, int, str]]:
    """公开面第二出口：提交信息（`git log %B`）＋ 标签注解（`git tag -n`）。

    工作树干净不代表这两个出口干净——commit message 与 tag 注解一旦推上 GitHub 就是公开内容，
    且清理只能改写历史（属不可逆例外）。所以这里的纪律是**基线只减不增**。
    """
    lines: list[tuple[str, int, str]] = []
    log = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00", "--no-merges"],
        cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=True,
    ).stdout
    for rec in [r for r in log.split("\0\0") if r.strip()]:
        parts = rec.split("\0", 1)
        if len(parts) != 2:
            continue
        sha, body = parts
        for idx, line in enumerate(body.splitlines(), start=1):
            lines.append((f"git:commit:{sha[:8]}", idx, line))
    tag = subprocess.run(
        ["git", "tag", "-n", "99"],
        cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=True,
    ).stdout
    for idx, line in enumerate(tag.splitlines(), start=1):
        lines.append(("git:tag-annotations", idx, line))
    return lines


def _release_text_lines(root: Path) -> list[tuple[str, int, str]]:
    """公开面第三出口：GitHub Release 正文（`gh release view`）。gh 不可用/未登录时如实报跳过。"""
    import os
    import shutil

    if shutil.which("gh") is None:
        print("  · Release 正文：本机无 gh，跳过（CI 步骤同理，工作树＋git 对象两闸仍必跑）")
        return []
    lines: list[tuple[str, int, str]] = []
    try:
        listing = subprocess.run(
            ["gh", "release", "list", "--limit", "30"],
            cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        print("  · Release 正文：gh 调用异常，跳过")
        return []
    if listing.returncode != 0:
        print("  · Release 正文：gh 未就绪（无网络或未登录），跳过")
        return []
    for row in listing.stdout.splitlines():
        name = row.split("\t")[0].strip()
        if not name:
            continue
        body = subprocess.run(
            ["gh", "release", "view", name, "--json", "body", "--jq", ".body"],
            cwd=root, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
        )
        if body.returncode != 0:
            continue
        for idx, line in enumerate(body.stdout.splitlines(), start=1):
            lines.append((f"gh:release:{name}", idx, line))
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="公开面泄露闸（工作树 tracked 文本 ＋ 可选 git 对象/Release）")
    parser.add_argument(
        "--git-text",
        action="store_true",
        help="额外扫提交信息／标签注解／Release 正文（历史既有命中按基线记账，只减不增）",
    )
    args = parser.parse_args(argv)

    files = iter_tracked_files()
    py_files = [p for p in files if p.suffix.lower() == ".py"]
    findings = scan_files(files)
    print(f"公开面泄露闸：扫描 tracked 文本 {len(files)} 个（其中 *.py {len(py_files)} 个）")
    rc = scan_findings(findings, stage="工作树")

    if args.git_text:
        git_lines = _git_text_lines(ROOT) + _release_text_lines(ROOT)
        git_findings = scan_lines(git_lines)
        print(f"公开面泄露闸（第二／第三出口）：git 文本行 {len(git_lines)} 条，命中 {len(git_findings)} 条"
              f"（基线 {GIT_TEXT_BASELINE}，只减不增）")
        if len(git_findings) > GIT_TEXT_BASELINE:
            for row in git_findings[:GIT_TEXT_BASELINE + 10]:
                print(f"  ✗ [git] {row['file']}:{row['line']} 命中「{row['rule']}」（命中文本不回显）")
            print(
                f"\ngit 对象/Release 命中数 {len(git_findings)} 超过基线 {GIT_TEXT_BASELINE} —— "
                "新写进去的线索必须删掉（历史既有部分不改写，见 docs/security.md §⑦）"
            )
            return 1
        if git_findings:
            print(f"  · 历史既有命中 {len(git_findings)} 条 ≤ 基线（公开仓历史不改写，只保证不新增）")
        else:
            print("  ✓ git 对象与 Release 正文：零命中")
    if rc:
        print("\n公开面扫描：有命中（必须脱敏或改由环境变量注入正本路径）")
        return 1
    print("✓ 公开面扫描：零命中（无凭据定位线索、无仓外正本路径、无本机账号痕迹）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
