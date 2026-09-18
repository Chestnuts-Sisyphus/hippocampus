"""可求证机制（七轮 T2 · 正本 §三-4 设计稿的施工）。

一句话判据：**一条内容算可求证，当且仅当本机存在一个机械判据（不需要人、不需要模型）
能把它判成"对／不对／查不了"**。拿不出判据的就是不可求证——**不可求证 ≠ 可疑**，
只是"本机判不了"（"可疑"是安全守卫 `security_flag` 的职责，两者不混）。

三级手段（按成本从低到高）：

| 级 | 判据 | 成本 | 默认 |
|---|---|---|---|
| L1 存在性／自洽性 | 路径存在、URL 合法、日期是真实日历日、百分数在区间内 | 零（纯本地） | **开** |
| L2 库内一致性 | 与库里同对象既有取值比对（复用 `conflict.detect_rule_conflicts`） | 零 | **开** |
| L3 外站探测 | 对 http(s) 链接发一次只读探测，只记状态码 | 一次出站 | **关**（`verification_external` 显式开） |

三态处置（**不是"存／不存"两态**）：

- `verified`   判据通过 → 直存，附手段／时间／证据摘要；
- `refuted`    明确否证 → 不进正式记忆：来源=用户 → 挂起一次询问（candidate＋TTL，
               **不静默丢**，用户可能指另一台机器／未来会创建）；来源=模型 → 丢弃＋观察日志；
- `unverifiable` 本机判不了 → **直接存储**（记忆是"用户说过什么"的记录，不是知识库）。

两条边界（防越权）：
1. **求证不替代确认轨**——能机械判的不用问人，需要人拍板的机器判不了，两者交集为空；
2. **求证不改变"记忆只增不删"**——`refuted` 的处置是不进正式记忆或挂起，不删既有记忆。

纪律：纯机械、零模型、零字面量凭据；L3 关闭时**零出站**（A45 由测试钉住）。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

VERIFIED = "verified"
REFUTED = "refuted"
UNVERIFIABLE = "unverifiable"

# 绝对路径（盘符或 POSIX）＋扩展名——与 `memory_bridge._resource_plausible` 同源口径：
# 只校验"完整绝对路径"声明，裸文件名不校验（引用用户环境里的真实文件，误杀风险高）。
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][\w\-.\\/ ~%]+\.\w{1,6})" r"|(?:/[\w\-.\\/ ~%]+\.\w{1,6})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>）)，。；]+", re.IGNORECASE)
# 日历日：2026-09-19 / 2026/9/19 / 2026年9月19日
_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
# 百分数（前面不是数字/字母，避免误抓 "v1.5%" 之类版本号碎片）
_PERCENT_RE = re.compile(r"(?<![\w.])\d{1,4}(?:\.\d+)?\s*%")


@dataclass
class VerificationResult:
    """一次求证的结果（写进 memories 的四列，可审计）。"""

    status: str = UNVERIFIABLE
    method: str = ""            # 命中的判据名，如 "L1:path_exists"／"L1:url_valid"
    evidence: str = ""          # 证据摘要（**只记摘要，不落内容**）
    checked_at: int = 0
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # L2：(旧记忆 id, 原因)

    @property
    def refuted(self) -> bool:
        return self.status == REFUTED


def _paths_in(content: str) -> list[str]:
    """绝对路径声明。先挖掉 URL 覆盖的区间——否则 POSIX 分支会把
    `http://127.0.0.1:9/x` 里的 `/127.0.0.1:9/x` 当成一条不存在的路径，误否证。"""
    text = content or ""
    for m in _URL_RE.finditer(text):
        text = text.replace(m.group(0), " " * len(m.group(0)))
    return [m.group(0).strip().strip("\"'") for m in _PATH_RE.finditer(text)]


def _urls_in(content: str) -> list[str]:
    return [m.group(0) for m in _URL_RE.finditer(content or "")]


def classify(content: str, kind: str) -> dict[str, Any]:
    """这条内容有没有本机机械判据；有哪些判据（不执行，供调用方决定是否求证）。"""
    checks: list[str] = []
    if kind == "preference":
        # 价值判断没有真值可查——求证机制**不碰偏好**（偏好的冲突归确认轨）
        return {"verifiable": False, "checks": []}
    text = content or ""
    if _paths_in(text):
        checks.append("path_exists")
    if _urls_in(text):
        checks.append("url_valid")
    if _DATE_RE.search(text):
        checks.append("date_possible")
    if _PERCENT_RE.search(text):
        checks.append("number_in_range")
    return {"verifiable": bool(checks), "checks": checks}


def _check_paths(paths: list[str]) -> tuple[bool, str]:
    if not paths:
        return True, "无绝对路径声明（裸文件名/相对路径不做文件系统校验）"
    existing = [p for p in paths if Path(p).exists()]
    if existing:
        return True, f"存在 {len(existing)}/{len(paths)} 条声明路径"
    return False, f"声明路径本机全不存在（{len(paths)} 条）"


def path_claims_plausible(content: str) -> bool:
    """只跑 L1 的路径判据，返回"可不可能为真"。

    存在的意义：把观察轨那条**前身时代的**幻觉资源校验（`memory_bridge._resource_plausible`，
    HC-0815-02 节点2）收敛到统一入口——同一份判据，别再长出第二套。口径保持逐字一致：
    只校验"完整绝对路径"声明，裸文件名放行（引用用户环境里的真实文件，误杀风险高）。"""
    return _check_paths(_paths_in(content or ""))[0]


def _check_urls(urls: list[str]) -> tuple[bool, str]:
    from hippocampus.net import validate_outbound_url

    bad: list[str] = []
    for url in urls:
        try:
            validate_outbound_url(url)
        except Exception as e:
            bad.append(f"{url.split('://', 1)[-1][:40]}: {e}")
    if bad:
        return False, "；".join(bad)
    return True, f"{len(urls)} 条链接合法（http/https、非环回/私有/保留）"


def _check_dates(content: str) -> tuple[bool, str]:
    for year, month, day in _DATE_RE.findall(content):
        try:
            datetime(int(year), int(month), int(day))
        except ValueError:
            return False, f"日历日不存在：{year}-{month}-{day}"
    return True, "日期均为真实日历日"


def _check_numbers(content: str) -> tuple[bool, str]:
    for raw in _PERCENT_RE.findall(content):
        value = float(re.sub(r"[^\d.]", "", raw) or 0)
        if value > 100.0:
            return False, f"百分数越界：{raw.strip()}"
    return True, "百分数在 0–100 区间"


# ---------------- L3：外站探测（默认关） ----------------

# 进程内缓存：同一 URL 在窗口内不重复探测（纪律：不重复出站轰炸）
_L3_CACHE: dict[str, tuple[float, int]] = {}
_L3_CACHE_TTL_S = 600.0


def _probe_url(url: str, timeout_s: float = 5.0) -> int | None:
    """发一次只读 HEAD（失败退回 GET）。返回状态码；网络异常返回 None（=未取证，不是"假"）。"""
    import urllib.request

    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "hippocampus-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310（URL 已过 validate_outbound_url）
            return int(resp.status)
    except Exception:
        return None


def probe_external(url: str) -> int | None:
    """L3 唯一出口：先过出站闸，再探测；命中缓存不重复出网。**只记状态码，不落内容**。"""
    from hippocampus.net import validate_outbound_url

    cached = _L3_CACHE.get(url)
    if cached and (time.time() - cached[0]) < _L3_CACHE_TTL_S:
        return cached[1]
    try:
        validate_outbound_url(url)
    except Exception:
        return None
    code = _probe_url(url)
    if code is not None:
        _L3_CACHE[url] = (time.time(), code)
    return code


def clear_external_cache() -> None:
    """清 L3 缓存（测试与长驻进程换配置用）。"""
    _L3_CACHE.clear()


# ---------------- 主入口 ----------------


def verify(
    content: str,
    kind: str,
    *,
    conn: sqlite3.Connection | None = None,
    source: str = "user",
    external: bool = False,
) -> VerificationResult:
    """跑三级判据，返回三态结果。**纯机械、零模型、任何异常软失败为 unverifiable**。"""
    text = (content or "").strip()
    if not text:
        return VerificationResult(status=UNVERIFIABLE, evidence="空内容")
    try:
        return _verify_inner(text, kind, conn=conn, source=source, external=external)
    except Exception as e:  # 求证坏掉绝不阻断写入主流程（与去重/守卫同一纪律）
        sys.stderr.write(f"[verification] 求证软失败（按不可求证处理）: {e}\n")
        return VerificationResult(status=UNVERIFIABLE, evidence=f"判据异常：{type(e).__name__}")


def _verify_inner(
    text: str, kind: str, *, conn: sqlite3.Connection, source: str, external: bool
) -> VerificationResult:
    paths = _paths_in(text)
    urls = _urls_in(text)
    methods: list[str] = []
    evidence: list[str] = []

    # ---- L1：存在性／自洽性 ----
    if paths:
        ok, detail = _check_paths(paths)
        if not ok:
            return VerificationResult(
                status=REFUTED, method="L1:path_exists", evidence=detail, checked_at=_now_ms()
            )
        methods.append("L1:path_exists")
        evidence.append(detail)
    if urls:
        ok, detail = _check_urls(urls)
        if not ok:
            return VerificationResult(
                status=REFUTED, method="L1:url_valid", evidence=detail, checked_at=_now_ms()
            )
        methods.append("L1:url_valid")
        evidence.append(detail)
        if external:
            codes = [(u, probe_external(u)) for u in urls]
            dead = [u for u, c in codes if c is not None and c >= 400]
            unknown = [u for u, c in codes if c is None]
            methods.append("L3:external_probe")
            evidence.append(
                f"外站探测：{len(codes) - len(dead) - len(unknown)} 可达"
                + (f"、{len(dead)} 返回 4xx/5xx" if dead else "")
                + (f"、{len(unknown)} 未取证" if unknown else "")
            )
            if dead:
                return VerificationResult(
                    status=REFUTED,
                    method=";".join(methods),
                    evidence="；".join(evidence),
                    checked_at=_now_ms(),
                )
    if _DATE_RE.search(text):
        ok, detail = _check_dates(text)
        if not ok:
            return VerificationResult(
                status=REFUTED, method="L1:date_possible", evidence=detail, checked_at=_now_ms()
            )
        methods.append("L1:date_possible")
        evidence.append(detail)
    if _PERCENT_RE.search(text):
        ok, detail = _check_numbers(text)
        if not ok:
            return VerificationResult(
                status=REFUTED, method="L1:number_in_range", evidence=detail, checked_at=_now_ms()
            )
        methods.append("L1:number_in_range")
        evidence.append(detail)

    if not methods:
        # 没有任何可校验结构（"我是班长""我在天大"）→ 不可求证，**直接存储、不打可疑**
        reason = "偏好无真值可查" if kind == "preference" else "内容里没有本机可机械校验的结构"
        return VerificationResult(status=UNVERIFIABLE, evidence=reason)

    # ---- L2：库内一致性（只在 L1 没否证时跑；冲突不改语义，交给确认轨） ----
    if conn is not None:
        try:
            from hippocampus.memory import conflict as conflict_mod

            found = conflict_mod.detect_rule_conflicts(conn, text, kind, also_types=("preference", "fact"))
            for old_id, _placeholder, reason in found:
                methods.append("L2:库内一致性")
                evidence.append(f"与既有记忆冲突：{reason}")
                result_conflicts = [(old_id, reason)]
                return VerificationResult(
                    status=VERIFIED,
                    method=";".join(methods),
                    evidence="；".join(evidence),
                    checked_at=_now_ms(),
                    conflicts=result_conflicts,
                )
        except Exception as e:
            sys.stderr.write(f"[verification] L2 跳过（软失败）: {e}\n")
        evidence.append("库内无同对象冲突取值")
    return VerificationResult(status=VERIFIED, method=";".join(methods), evidence="；".join(evidence), checked_at=_now_ms())


def _now_ms() -> int:
    from hippocampus.memory import database as db

    return db.now_ms()


__all__ = [
    "REFUTED",
    "UNVERIFIABLE",
    "VERIFIED",
    "VerificationResult",
    "classify",
    "clear_external_cache",
    "path_claims_plausible",
    "probe_external",
    "verify",
]
