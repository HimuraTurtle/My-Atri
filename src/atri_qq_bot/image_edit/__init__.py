"""ATRI 的修图能力（确定性命令，不经聊天模型）。

第一性原理：对称化是纯像素操作（crop + 翻转 + paste），模型在这里
只会引入幻觉和延迟——触发词 + 引用图片的强前置信号让确定性拦截比
LLM 工具调用更可靠，且零成本。
"""

from __future__ import annotations

from .fetch import download_image, first_image_segment, load_segment_image_bytes
from .gif import is_animated, write_mirrored_gif
from .mirror import mirror_image, mirror_left_to_right
from .trigger import is_symmetry_request, parse_symmetry_request

__all__ = [
    "download_image",
    "first_image_segment",
    "load_segment_image_bytes",
    "is_animated",
    "write_mirrored_gif",
    "mirror_image",
    "mirror_left_to_right",
    "is_symmetry_request",
    "parse_symmetry_request",
]
