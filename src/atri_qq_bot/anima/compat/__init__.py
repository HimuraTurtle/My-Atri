"""AstrBot 接口兼容垫片。

anima 插件包源自 astrbot_plugin_anima_master，内部代码以 AstrBot 的
Context / AstrMessageEvent / message_components 为协作对象。本包提供
等价的替身实现，把这三类依赖分别桥接到 ATRi 的 LLM 客户端、会话
上下文与 OneBot 消息投递，使插件包保持原逻辑零改动（除 import 行）。
"""

from .components import Comp, Image, Reply
from .context import CompatContext, LLMResponse, tavily_search
from .event import CompatEvent
from .media import MediaResolver, OneBotClient

__all__ = [
    "Comp",
    "Image",
    "Reply",
    "CompatContext",
    "CompatEvent",
    "LLMResponse",
    "MediaResolver",
    "OneBotClient",
    "tavily_search",
]
