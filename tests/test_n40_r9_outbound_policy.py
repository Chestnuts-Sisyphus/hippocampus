"""九轮 W3：出站口径归一（缺口 K4）——文档／代码／测试三处必须逐字同一口径。

开工复核到的真实不一致（三方各说各话）：
- 文档侧早就写"**仅 https**"（`docs/deployment.md` §三第 4 条、§二·五第 3 条、`docs/roadmap.md` E1 行）；
- 实现侧 `validate_outbound_url` 却放行 `http://`（`ALLOWED_SCHEMES = {http, https}`）；
- 测试侧更把这条写成**期望行为**（`test_outbound_url_allows_public_http` 用 `http://example.com:8080` 断言放行）
  ——假绿的教科书样本：闸没拦，测试还替它背书。

本轮把三处对齐成一句话：**数据/模型提供的 URL 默认仅 https，明文公网出口要显式开关（默认关）；
操作员配置的本地端点走另一条通道，允许环回 http。**

本文件负责"文档↔实现"逐字核对（口径漂移即红）；行为回归在 `tests/test_a40_a41_safety.py`。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hippocampus import net

REPO = Path(__file__).resolve().parents[1]

# 说这条口径的文档正本（缺一个就说明有人在文档里松了闸）
DOC_FILES = (
    "docs/security.md",
    "docs/deployment.md",
    "docs/roadmap.md",
    "README.zh-CN.md",
    "README.md",
    "src/hippocampus/net.py",
)

# 出站通道 + 明文 http 放行 的旧口径写法（实现改回去、或文档被松口，都会命中）
_STALE = re.compile(r"(仅|只)\s*允许\s*http\s*[／/]?\s*https|http/https")


def stale_plaintext_lines(text: str) -> list[str]:
    """返回"把出站通道说成允许 http"的行；干净口径下应为空。"""
    hits = []
    for line in text.splitlines():
        if "validate_outbound_url" in line and _STALE.search(line):
            hits.append(line.strip())
    return hits


def test_outbound_channel_is_https_only_by_default():
    assert net.OUTBOUND_SCHEMES == frozenset({"https"})
    with pytest.raises(net.UnsafeURLError):
        net.validate_outbound_url("http://example.com/page")


def test_plaintext_switch_defaults_off_and_is_explicit(monkeypatch):
    """明文公网出口必须是**显式**开关，且默认关；开关名要在文档里能搜到。"""
    monkeypatch.delenv(net.PLAINTEXT_OUTBOUND_ENV, raising=False)
    assert net.plaintext_outbound_allowed() is False
    monkeypatch.setenv(net.PLAINTEXT_OUTBOUND_ENV, "1")
    assert net.plaintext_outbound_allowed() is True
    docs = "".join((REPO / p).read_text(encoding="utf-8") for p in DOC_FILES)
    assert net.PLAINTEXT_OUTBOUND_ENV in docs, "开关没有写进任何文档正本"


def test_local_endpoint_channel_still_allows_loopback_http():
    """本地模型端点＝操作员配置，走 `validate_endpoint_url`，环回 http 必须仍可用。"""
    assert net.validate_endpoint_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert net.validate_endpoint_url("http://localhost:8000/v1") == "http://localhost:8000/v1"


@pytest.mark.parametrize("relpath", DOC_FILES)
def test_docs_state_the_same_outbound_rule(relpath):
    """口径一致：说这条规则的每个正本都要写"仅 https"，且不得再把明文 http 说成放行。

    分两类：技术正本（`docs/*`、`src/hippocampus/net.py`）点名 `validate_outbound_url`，
    逐行核对；对外文案（README 双语）不提内部函数名，只核口径措辞。
    """
    text = (REPO / relpath).read_text(encoding="utf-8")
    if "validate_outbound_url" in text:
        outbound_lines = [ln for ln in text.splitlines() if "validate_outbound_url" in ln or "出站" in ln]
        assert outbound_lines, f"{relpath} 里没有出站口径的行"
        joined = "\n".join(outbound_lines)
        assert "仅 https" in joined or "https-only" in joined, f"{relpath} 的出站口径没写仅 https"
        assert not stale_plaintext_lines(text), f"{relpath} 又把明文 http 说成放行：{stale_plaintext_lines(text)}"
    else:
        assert "https-only" in text or "仅 https" in text or "仅允许 https" in text, (
            f"{relpath} 未同步出站口径"
        )


def test_gate_turns_red_on_doc_or_code_drift():
    """对照测试：植入"仅 http/https"的旧口径 → 闸必须报出那一行（防止文档被人松口）。"""
    planted = "## ② 出站 URL\n| 数据/模型提供的 URL | `validate_outbound_url` | 仅 http/https；拒绝环回 |\n"
    hits = stale_plaintext_lines(planted)
    assert len(hits) == 1, f"植入旧口径未被逮住：{hits}"
    # 当前实现若被回退成放行 http（OUTBOUND_SCHEMES 加回 http），本条即红
    assert "http" not in net.OUTBOUND_SCHEMES
