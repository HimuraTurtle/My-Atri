"""Milky 连接配置：从环境变量读取，避免硬编码。

环境变量：
- ``MILKY_BASE_URL``：Lagrange.Milky 的 HTTP 地址（默认 http://127.0.0.1:8081）。
- ``MILKY_ACCESS_TOKEN``：Milky 的访问令牌；留空表示不鉴权（仅限本机）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BASE_URL = "http://127.0.0.1:8081"


@dataclass(frozen=True)
class MilkyConfig:
    base_url: str
    access_token: str = ""

    @classmethod
    def from_env(cls) -> "MilkyConfig":
        base_url = (os.getenv("MILKY_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        access_token = (os.getenv("MILKY_ACCESS_TOKEN") or "").strip()
        return cls(base_url=base_url, access_token=access_token)

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api"

    @property
    def event_url(self) -> str:
        url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        if self.access_token:
            return f"{url}/event?access_token={self.access_token}"
        return f"{url}/event"

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
