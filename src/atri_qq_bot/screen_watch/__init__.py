"""ATRI 的屏幕看护能力（自建轻量管道）。

设计原则（第一性原理）：
- 屏幕是连续像素流，但「主人在做什么」的语义变化是分钟级的——
  用便宜的本地帧差分决定何时调用昂贵的视觉模型；
- 屏幕是全机最敏感的数据源——敏感窗口黑名单 + 文件级急停 + 摘要
  不落原图，全部隐私闸默认内置；
- 视觉理解复用既有 TOOLBOX_VISION_* 配置（openai-compat 通道），
  不重复建设模型客户端。
"""

from __future__ import annotations

from .capture import capture_screen, encode_jpeg, gray_small
from .privacy import PauseFlag, active_window_title, is_sensitive_title
from .store import ScreenWatchStore
from .trigger import FrameTrigger
from .vision import describe_screen
from .watcher import ScreenWatcher

__all__ = [
    "capture_screen",
    "encode_jpeg",
    "gray_small",
    "PauseFlag",
    "active_window_title",
    "is_sensitive_title",
    "ScreenWatchStore",
    "FrameTrigger",
    "describe_screen",
    "ScreenWatcher",
]
