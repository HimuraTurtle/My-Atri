"""MilkyTransport：把 Lagrange.Milky 包装成 ATRI OneBotServer 期望的
「websocket 对象」。

ATRI 的 ``handle_connection`` 通过 ``async for raw in websocket`` 收事件、
通过 ``websocket.send(payload)`` 发动作。MilkyTransport 把前者映射到 Milky
``/event`` WebSocket，把后者映射到 Milky ``/api/:api`` HTTP POST，并负责
动作回包的 echo 关联。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import websockets

from .actions import shape_response, translate_action
from .config import MilkyConfig
from .protocol import event_milky_to_onebot


class MilkyTransport:
    def __init__(self, config: MilkyConfig, server: Any) -> None:
        self._config = config
        self._server = server
        self._http: httpx.AsyncClient | None = None
        self._ws: Any = None
        self.remote_address = "milky"

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._config.event_url)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def send(self, raw: str) -> None:
        """执行一个 OneBot 动作：翻译 -> HTTP POST -> 回包并解析 echo。"""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        action = str(payload.get("action") or "")
        echo = str(payload.get("echo") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

        translated = translate_action(action, params)
        if translated is None:
            self._resolve(echo, {"status": "failed", "retcode": -1, "data": {}, "message": "不支持的 action"})
            return
        milky_api, milky_body, context = translated
        milky_resp = await self._post(milky_api, milky_body)
        self._resolve(echo, shape_response(action, echo, milky_resp, context))

    def _resolve(self, echo: str, onebot_resp: dict[str, Any]) -> None:
        resolver = getattr(self._server, "_resolve_pending_action", None)
        if callable(resolver):
            resolver({**onebot_resp, "echo": echo})

    async def _post(self, api: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self._config.api_url}/{api}"
        try:
            resp = await self._http.post(url, json=body, headers=self._config.auth_headers)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"status": "failed", "retcode": -1, "data": {}, "message": "无效响应"}
        except httpx.HTTPStatusError as exc:
            return {"status": "failed", "retcode": -1, "data": {}, "message": f"HTTP {exc.response.status_code}"}
        except Exception as exc:  # 网络/解析异常统一降级为失败回包
            return {"status": "failed", "retcode": -1, "data": {}, "message": str(exc)}

    def __aiter__(self) -> "MilkyTransport":
        return self

    async def __anext__(self) -> str:
        if self._ws is None:
            raise StopAsyncIteration
        while True:
            try:
                raw = await self._ws.recv()
            except websockets.exceptions.ConnectionClosed:
                raise StopAsyncIteration
            try:
                milky_event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            onebot_event = event_milky_to_onebot(milky_event)
            if onebot_event is not None:
                return json.dumps(onebot_event, ensure_ascii=False)
