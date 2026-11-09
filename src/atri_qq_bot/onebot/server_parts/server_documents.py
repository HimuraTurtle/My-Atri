"""OneBot 层的通用文档投递：send_document 生成的文档发送。

文档由聊天模型通过 send_document 工具触发（自然语言指令），
工具执行时已落盘到 data/runtime/documents 并排入待投递队列；
本层在文字回复发出后消费队列真正发文件（与论文整理/漫画/绘画
同一模式）。
"""

from __future__ import annotations

import logging
from typing import Any

from ...message_plan import OutgoingMessage
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")


class OneBotDocumentsMixin(OneBotServerMixin):
    async def _deliver_pending_documents(
        self,
        websocket: Any,
        event: dict[str, Any],
        conversation_id: str,
    ) -> None:
        """消费 send_document 排队的文档并发送到当前会话。"""
        from ...documents import get_documents_service

        service = get_documents_service(self.config)
        if service is None:
            return
        for path in service.consume_deliveries(conversation_id):
            delivered = await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("file", str(path))],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
            if not delivered:
                LOGGER.warning(
                    "文档投递失败：conversation=%s file=%s",
                    conversation_id,
                    path,
                )
                await self.send_reply(
                    websocket,
                    event,
                    [OutgoingMessage("text", "文档发送失败了，稍后再让我试一次吧。")],
                    model_type="chat",
                    model_name=self._delivery_model_name("chat"),
                )
                return
