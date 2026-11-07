"""本机回环地址校验：OneBot WebSocket 与项目 WebUI 共用。

浏览器发起的请求总是带有 ``Origin`` 头，而 NapCat 这类非浏览器客户端通常
不发 ``Origin``。这里把「无 Origin / 回环 Origin / 回环 Host」判为可信，
用于防御跨站 WebSocket 劫持（CSWSH）、DNS 重绑定与本机 CSRF。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def _loopback_hostname(hostname: str) -> bool:
    name = str(hostname or "").strip().casefold().rstrip(".")
    if name in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(address.is_loopback)


def host_is_loopback(value: str | None) -> bool:
    """Host 头是否指向本机回环；缺失视为不可信（HTTP/1.1 必有 Host）。"""
    host = str(value or "").strip()
    if not host:
        return False
    try:
        parsed = urlsplit(f"//{host}")
    except ValueError:
        return False
    return _loopback_hostname(parsed.hostname or "")


def origin_is_loopback(value: str | None) -> bool:
    """Origin 头是否可信：缺失/空（非浏览器客户端）或回环来源视为可信。"""
    origin = str(value or "").strip().casefold()
    if not origin:
        return True
    if origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    return _loopback_hostname(parsed.hostname)
