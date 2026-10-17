"""astrbot.api.message_components 的替身。

插件只用到 Comp.Image 与 Comp.Reply 两个组件：
- 发送侧：Comp.Image.fromFileSystem(path) 构造待发图片，由
  CompatEvent.send 消费；convert_to_file_path 在下载保存时使用。
- 解析侧：从消息链里识别图片/引用组件。ATRi 侧的消息链由
  CompatEvent.get_messages() 提供，核心生图链路恒为空。
"""

from __future__ import annotations

from typing import Any


class Image:
    """图片消息组件替身。"""

    def __init__(
        self,
        file: str = "",
        url: str = "",
        path: str = "",
        **kwargs: Any,
    ) -> None:
        self.file = str(file or "")
        self.url = str(url or "")
        self.path = str(path or "")
        self._type = "image"
        self.chain: list[Any] = []
        for key, value in kwargs.items():
            if not hasattr(self, key):
                setattr(self, key, value)

    @classmethod
    def fromFileSystem(cls, path: str) -> "Image":
        return cls(file=str(path), path=str(path))

    async def convert_to_file_path(self) -> str:
        for candidate in (self.path, self.file):
            if candidate:
                return candidate
        raise RuntimeError("compat image component has no local file path")


class Reply:
    """引用消息组件替身。"""

    def __init__(self, id: str = "", chain: list[Any] | None = None, **kwargs: Any) -> None:
        self.id = str(id or "")
        self.chain = list(chain or [])
        for key, value in kwargs.items():
            if not hasattr(self, key):
                setattr(self, key, value)


class _Components:
    """astrbot.api.message_components 命名空间替身。"""

    Image = Image
    Reply = Reply


Comp = _Components
