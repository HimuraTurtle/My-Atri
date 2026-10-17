"""AstrBot 媒体工具替身。

MediaResolver 与 OneBotClient 只服务于图片输入解析（改图/引用图/
反推）。核心文生图链路里 CompatEvent.get_messages() 恒为空、
raw_message 为 None，不会触达这两个类的实际方法，这里只保证
import 与构造可用；真正启用图片输入时再替换为 ATRi 实现。
"""

from __future__ import annotations

from typing import Any


class MediaResolver:
    def __init__(
        self,
        ref: str,
        media_type: str = "image",
        default_suffix: str = ".bin",
        **kwargs: Any,
    ) -> None:
        self.ref = str(ref or "")
        self.media_type = media_type
        self.default_suffix = default_suffix

    def as_path(self) -> Any:
        raise RuntimeError("compat MediaResolver is a placeholder; image input is not wired")


class OneBotClient:
    def __init__(self, event: Any) -> None:
        self.event = event
