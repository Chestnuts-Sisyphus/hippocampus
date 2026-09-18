"""七轮 T9 守护：凭据扫描闸本身要有对照测试（闸没有对照 = 不知道它到底拦不拦）。

起因（A 级实测）：`scan_credentials.py` 的短密钥形状规则少了词首边界，把普通英文单词
`task-overlay` 里的子串（形如 sk-xxxxxxx）判成"疑似密钥字面量"，在另一会话的交接文档上假报警。
任务书 T9 原写"该行值→占位符"，实测证明它是**误报**，所以本轮改的是判据而不是别人的文档。

两侧都必须成立：
- **拦得住**：真凭据形态（独立串）必须命中——加词首边界不许降召回；
- **不冤枉**：普通词里含 `sk-` 子串、自带占位标注的行，不得命中。

（本文件里的假凭据一律用拼接写出，避免扫描闸命中自己的测试样例。）
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_credentials.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("scan_credentials", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _hits(text: str) -> list[dict]:
    """把一行文本写成临时 .md 后过真实入口 `scan_text`（不测私有实现）。"""
    scanner = _scanner()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.md"
        path.write_text(text, encoding="utf-8")
        return scanner.scan_text(path)


def test_real_credential_shapes_are_caught():
    """召回侧：三种真凭据形态都必须命中。"""
    cases = {
        "openai_sk": "OPENAI_KEY=" + "sk-" + "abc123def456ghi789",
        "github_pat": "token = " + "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "private_key": "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5,
    }
    for name, line in cases.items():
        assert _hits(line), f"真凭据形态漏检：{name}"


def test_word_substring_is_not_a_false_positive():
    """回归（七轮 T9）：`task-overlay` 这类普通词不该触发密钥告警。"""
    line = ("timeline-narrative）+ visual + task" + "-overlay + decoration + edit/guide "
            "+ markdown-grammar")
    assert _hits(line) == [], "短密钥形状规则又丢了词首边界（会假报警）"


def test_placeholder_marked_line_is_allowed():
    line = "api_key = " + "sk-" + "examplevalue001（占位符 placeholder，非真值）"
    assert _hits(line) == []


def test_repo_scan_is_clean_at_head():
    """发布门槛：整仓可扫文件必须零命中（扫描面走脚本自己的 iter_files）。"""
    repo = Path(__file__).resolve().parents[1]
    scanner = _scanner()
    findings: list[dict] = []
    for path in scanner.iter_files(repo / "src"):
        findings.extend(scanner.scan_text(path))
    for path in scanner.iter_files(repo / "scripts"):
        findings.extend(scanner.scan_text(path))
    for path in scanner.iter_files(repo / "tests"):
        findings.extend(scanner.scan_text(path))
    places = [f"{item['file']}:{item['line']}" for item in findings[:5]]
    assert not findings, f"扫描命中：{places}"
