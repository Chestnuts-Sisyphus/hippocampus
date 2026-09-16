"""出站 URL 校验（验收 A41 · 安全硬约束②）。

两条不同的通道，**规则不同**（方案 v2 §二十-①）：

| 通道 | 来源 | 规则 |
|---|---|---|
| `validate_outbound_url` | **数据或模型提供**的 URL（工具参数、记忆内容里的链接、抓取目标） | 仅 http/https；**拒绝 localhost／环回／私有／保留地址**（含 IPv6） |
| `validate_endpoint_url` | **操作员配置**的端点（模型 base_url、本代理监听地址） | 仅 http/https；允许环回（本地模型端点合法） |

判定用 `ipaddress` 标准库，不靠字符串前缀匹配——`http://127.0.0.1`、`http://[::1]`、
`http://0x7f000001`、`http://2130706433` 这类写法都要拦住。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

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


def validate_url(url: str, *, allow_loopback: bool) -> str:
    """统一校验入口。返回规范化 URL；不通过抛 UnsafeURLError。"""
    text = (url or "").strip()
    if not text:
        raise UnsafeURLError("URL 为空")
    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"仅允许 http/https（收到 {scheme or '空 scheme'}）: {text[:120]}")
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
    """数据/模型提供的 URL：仅 http/https，拒环回／私有／保留。"""
    return validate_url(url, allow_loopback=False)


def validate_endpoint_url(url: str) -> str:
    """操作员配置的端点：仅 http/https（允许环回，本地模型端点合法）。"""
    return validate_url(url, allow_loopback=True)
