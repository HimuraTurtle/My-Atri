"""OneBot 层的「是啊，吃什么」接梗（移植自 astrbot_plugin_what_to_eat）。

群聊被动触发：任意成员消息含「吃什么/吃啥」即按概率推荐食物或复读
「是啊，吃什么」，不要求 @ 机器人。与 B站自动总结一样放在回复门控
之前——原插件监听全部消息，接梗正是要在没人 @ 的时候冒出来。
私聊不接梗：罐头回复不能替代人设对话。
"""
from __future__ import annotations

import logging
from typing import Any

from ...bilibili.parsing import strip_address_prefix
from ...message_plan import OutgoingMessage
from ...what_to_eat import get_what_to_eat_service
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

# 确定性命令的参数里可能带关键词（如「漫画 搜索 今天吃什么」），
# 命令入口在回复门控之后也能接住，接梗在消息以这些前缀开头时让位。
_COMMAND_PREFIX_YIELD = ("漫画", "B站", "头像识别")


class OneBotWhatToEatMixin(OneBotServerMixin):
    @property
    def what_to_eat(self) -> Any | None:
        if not bool(getattr(self.config, "what_to_eat_enabled", True)):
            return None
        service = getattr(self, "_what_to_eat_service", None)
        if service is None:
            service = get_what_to_eat_service(self.config)
            self._what_to_eat_service = service
        return service

    async def _maybe_handle_what_to_eat(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中接梗时发送回复并返回 True，调用方直接结束本轮。"""
        service = self.what_to_eat
        if service is None or event.get("message_type") != "group":
            return False
        if not service.matches(plain_text):
            return False
        if strip_address_prefix(plain_text).startswith(_COMMAND_PREFIX_YIELD):
            return False
        trust_tier = getattr(self.reply_engine.memory, "trust_tier", None)
        if callable(trust_tier) and trust_tier(_profile_id(event)) == "blocked":
            return False

        decision = service.decide(conversation_id)
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", decision.text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("「吃什么」接梗回复发送失败：conversation=%s", conversation_id)
            return False
        self._mark_smart_group_reply(event, conversation_id, plain_text)
        self.reply_engine.record_bot_reply(conversation_id, decision.text, False)
        return True
