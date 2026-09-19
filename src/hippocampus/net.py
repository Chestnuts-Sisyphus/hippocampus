"""出站 URL 校验（验收 A41 · 安全硬约束②）。

两条不同的通道，**规则不同**（方案 v2 §二十-①）：

| 通道 | 来源 | 规则 |
|---|---|---|
| `validate_outbound_url` | **数据或模型提供**的 URL（工具参数、记忆内容里的链接、抓取目标） | **仅 https**（九轮 W3 收紧）；**拒绝 localhost／环回／私有／保留地址**（含 IPv6）。明文 `http://` 公网出口需显式设 `HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND=1`，**默认关** |
| `validate_endpoint_url` | **操作员配置**的端点（模型 base_url、本代理监听地址） | 仅 http/https；允许环回（本地模型端点合法，`http://127.0.0.1:11434` 可用） |

> 出站口径三处一致（文档／代码／测试）：`docs/deployment.md`、`docs/security.md`、`docs/roadmap.md`
> 与本文件 + `tests/test_a40_a41_safety.py` 必须同说"**仅 https**、明文公网要显式开闸"。

判定用 `ipaddress` 标准库，不靠字符串前缀匹配——`http://127.0.0.1`、`http://[::1]`、
`http://0x7f000001`、`http://2130706433` 这类写法都要拦住。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
# 九轮 W3（K4）：出站通道**默认只允许 https**。文档三处（`docs/deployment.md`／`docs/security.md`／
# `docs/roadmap.md`）与实现、测试同一口径：**仅 https**，明文公网出口要显式开闸。
OUTBOUND_SCHEMES = frozenset({"https"})
PLAINTEXT_OUTBOUND_ENV = "HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND"

# 主机名字面量黑名单（ipaddress 覆盖不到的常见写法）
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
    }
)


class UnsafeURLError(ValueError):
    """URL 未通过出站校验。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None and _is_blocked_ip(ip.ipv4_mapped))
    )


def _parse(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把 host 解析成 IP（含十进制/十六进制整数写法）；域名返回 None。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:  # 单段整数写法 http://2130706433/
        return ipaddress.ip_address(int(host, 0))
    except (ValueError, TypeError):
        return None


def plaintext_outbound_allowed() -> bool:
    """明文（http）公网出口的**显式开关**，默认关（九轮 W3）。

    只对 `validate_outbound_url`（数据/模型提供的 URL）生效；操作员端点另有
    `validate_endpoint_url`，本来就允许环回 http。开闸方式：
    `HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND=1`（或 `true`/`yes`）。
    """
    return (os.environ.get(PLAINTEXT_OUTBOUND_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def validate_url(url: str, *, allow_loopback: bool, schemes: frozenset[str] = ALLOWED_SCHEMES) -> str:
    """统一校验入口。返回规范化 URL；不通过抛 UnsafeURLError。"""
    text = (url or "").strip()
    if not text:
        raise UnsafeURLError("URL 为空")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in schemes:
        allowed = "/".join(sorted(schemes))
        raise UnsafeURLError(f"仅允许 {allowed}（收到 {scheme or '空 scheme'}）: {text[:120]}")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeURLError(f"URL 缺少主机名: {text[:120]}")
    if not allow_loopback and host in _BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"拒绝访问本机/元数据主机名: {host}")
    ip = _parse(host)
    if ip is not None:
        if _is_blocked_ip(ip) and not allow_loopback:
            raise UnsafeURLError(f"拒绝访问环回/私有/保留地址: {host}")
    elif not allow_loopback:
        # 域名：解析一遍，避免 DNS 指向内网（解析失败不拦，交给真正的请求去报错）
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except OSError:
            return text
        for info in infos:
            addr = info[4][0]
            resolved = _parse(addr)
            if resolved is not None and _is_blocked_ip(resolved):
                raise UnsafeURLError(f"域名 {host} 解析到环回/私有/保留地址 {addr}，拒绝")
    return text


def validate_outbound_url(url: str) -> str:
    """数据/模型提供的 URL：**仅 https**，拒环回／私有／保留。

    明文 `http://` 公网出口默认被拒（九轮 W3）；确实需要时显式设
    `HIPPOCAMPUS_ALLOW_PLAINTEXT_OUTBOUND=1`（例如离线内网抓取）。
    """
    schemes = ALLOWED_SCHEMES if plaintext_outbound_allowed() else OUTBOUND_SCHEMES
    return validate_url(url, allow_loopback=False, schemes=schemes)


def validate_endpoint_url(url: str) -> str:
    """操作员配置的端点：http/https 都可（本地模型/镜像走环回 http 是合法用法）。"""
    return validate_url(url, allow_loopback=True, schemes=ALLOWED_SCHEMES)
