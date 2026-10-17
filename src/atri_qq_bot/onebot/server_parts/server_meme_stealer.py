"""OneBot 层的表情包小偷拦截（移植自 astrbot_plugin_stealer）。

被动偷图：任意群/私聊消息携带表情包类 segment 即按门控入后台队列，
下载与 VLM 分类全部在 worker 里进行，不阻塞回复链路。与「是啊，
吃什么」相同，放在回复门控之前——偷图监听全部消息，不需要 @ 机器人。
"""

from __future__ import annotations

import logging
from typing import Any

from ...meme_stealer import get_meme_steal_service
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")


class OneBotMemeStealMixin(OneBotServerMixin):
    @property
    def meme_stealer(self) -> Any | None:
        if not bool(getattr(self.config, "meme_steal_enabled", True)):
            return None
        service = getattr(self, "_meme_steal_service", None)
        if service is None:
            service = get_meme_steal_service(self.config)
            self._meme_steal_service = service
        return service

    def _maybe_steal_stickers(self, event: dict[str, Any], plain_text: str) -> None:
        """同步门控 + 入队；真正的下载/分类在后台 worker 里。"""
        service = self.meme_stealer
        if service is None:
            return
        # 只偷群聊：私聊图片（尤其相册直发照片）更敏感，不进云 VLM。
        if str(event.get("message_type") or "") != "group":
            return
        conversation_id = str(event.get("group_id") or event.get("user_id") or "")
        try:
            service.maybe_steal_from_event(
                event,
                context_text=plain_text,
                conversation_id=conversation_id,
            )
        except Exception:
            LOGGER.exception("表情包偷取入队失败")

    async def shutdown_meme_stealer(self) -> None:
        service = getattr(self, "_meme_steal_service", None)
        if service is not None:
            await service.shutdown()
            self._meme_steal_service = None
