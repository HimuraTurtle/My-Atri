"""QQ 空间会话：从 NapCat 按需借 Cookie，不独立登录、不常驻保活。

移植自 astrbot_plugin_qzone 的 QzoneSession，关键差异：
- Cookie 来源是 OneBot ``get_cookies(domain="user.qzone.qq.com")``，会话
  完全复用 NapCat 唯一登录会话，进程内只做 TTL 缓存，无任何心跳请求；
- 失效自愈：单次请求失败时让调用方 invalidate 后重试一次。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger("atri.qzone")

DOMAIN = "user.qzone.qq.com"

CookieProvider = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass
class QzoneContext:
    uin: int
    skey: str
    p_skey: str

    @property
    def gtk2(self) -> str:
        """QQ 空间 g_tk：对 p_skey 做 djb2 变换取低 31 位。"""
        hash_val = 5381
        for char in self.p_skey:
            hash_val += (hash_val << 5) + ord(char)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        return {"uin": f"o{self.uin}", "skey": self.skey, "p_skey": self.p_skey}

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://{DOMAIN}/{self.uin}",
            "Origin": f"https://{DOMAIN}",
        }


class QzoneSession:
    """Cookie 上下文缓存；cookie_provider 由 OneBot 服务端注入。"""

    def __init__(
        self,
        cookie_provider: CookieProvider,
        *,
        ttl_seconds: float = 600.0,
    ) -> None:
        self._cookie_provider = cookie_provider
        self._ttl = max(0.0, float(ttl_seconds))
        self._ctx: QzoneContext | None = None
        self._refreshed_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if self._ctx is None or self._is_expired():
                self._ctx = await self._refresh_locked()
            return self._ctx

    async def get_uin(self) -> int:
        return (await self.get_ctx()).uin

    async def invalidate(self) -> None:
        async with self._lock:
            self._ctx = None
            self._refreshed_at = 0.0

    def _is_expired(self) -> bool:
        if self._ttl <= 0:
            return False
        if self._refreshed_at <= 0:
            return True
        return time.monotonic() - self._refreshed_at >= self._ttl

    async def _refresh_locked(self) -> QzoneContext:
        payload = await self._cookie_provider(DOMAIN)
        cookies_text = ""
        if isinstance(payload, dict):
            for key in ("cookies", "cookie"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    cookies_text = value.strip()
                    break
        if not cookies_text:
            raise RuntimeError(
                "未能从 NapCat 获取 QQ 空间 Cookie（get_cookies 未返回数据），"
                "请确认 NapCat 在线且开放 Cookies 上报"
            )
        jar = SimpleCookie(cookies_text)
        values = {key: item.value for key, item in jar.items()}
        uin_text = values.get("uin") or values.get("o_uin") or "0"
        if uin_text[:1].lower() == "o":
            uin_text = uin_text[1:]
        if not uin_text.isdigit() or not int(uin_text):
            raise RuntimeError("QQ 空间 Cookie 中缺少合法 uin，可能未完成空间登录")
        self._refreshed_at = time.monotonic()
        return QzoneContext(
            uin=int(uin_text),
            skey=values.get("skey") or "",
            p_skey=values.get("p_skey") or values.get("skey") or "",
        )
