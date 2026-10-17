"""AstrMessageEvent 替身。

CompatEvent 由 AnimaService 在每次工具调用时构造，携带 ATRi 会话
上下文（conversation_id / sender_id / group_id）。插件对 event 的
三类用法分别映射：
- 身份查询（get_sender_id 等）→ 直接返回 ATRi 侧字段；
- 即时发文本（send(plain_result)）→ 收集到 notices，由 anima 工具
  返回文本时融合，不在工具执行中直接触碰 OneBot；
- 发图片（send(chain_result([Comp.Image]))）→ 图片路径交给
  deliver 回调，由 AnimaService 排入待投递队列，OneBot 层在文字
  回复发出后统一发送（与 manga 投递同一模式）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .components import Image


@dataclass
class _EmptyMessage:
    raw_message: Any = None


@dataclass
class CompatEvent:
    conversation_id: str
    sender_id: str
    session_id: str = ""
    platform_id: str = "qq"
    group_id: str = ""
    deliver: Callable[[str], None] = field(default=lambda path: None)
    notices: list[str] = field(default_factory=list)
    delivered_images: list[str] = field(default_factory=list)
    _message_obj: _EmptyMessage = field(default_factory=_EmptyMessage)

    @property
    def unified_msg_origin(self) -> str:
        return f"qq:{self.session_id or self.conversation_id}"

    @property
    def message_obj(self) -> _EmptyMessage:
        return self._message_obj

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_session_id(self) -> str:
        return self.session_id or self.conversation_id

    def get_platform_id(self) -> str:
        return self.platform_id

    def get_group_id(self) -> str:
        return self.group_id

    def is_admin(self) -> bool:
        return False

    def get_messages(self) -> list[Any]:
        return []

    def plain_result(self, text: str) -> tuple[str, str]:
        return ("text", str(text or ""))

    def chain_result(self, chain: list[Any]) -> tuple[str, list[Any]]:
        return ("chain", list(chain or []))

    async def send(self, result: Any) -> None:
        kind, payload = result
        if kind == "text":
            text = str(payload or "").strip()
            if text:
                self.notices.append(text)
            return
        for component in payload:
            if not isinstance(component, Image):
                continue
            path = component.path or component.file
            if not path:
                continue
            self.delivered_images.append(str(path))
            self.deliver(str(path))
