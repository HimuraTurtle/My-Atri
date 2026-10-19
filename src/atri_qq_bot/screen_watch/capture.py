"""屏幕截图：一次采集派生两种用途（差分小图 + 视觉 JPEG）。"""

from __future__ import annotations

import io

import numpy as np

try:
    import mss
except Exception:  # pragma: no cover - 采集依赖缺失时上层按失败降级
    mss = None

from PIL import Image

GRAY_WIDTH = 160
GRAY_HEIGHT = 90
VISION_WIDTH = 1280
JPEG_QUALITY = 82


def capture_screen(monitor: int = 1) -> Image.Image | None:
    """抓取指定显示器全屏，返回 RGB PIL Image；失败返回 None。"""
    if mss is None:
        return None
    try:
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[monitor])
        raw = np.asarray(shot, dtype=np.uint8)[..., :3]
        return Image.fromarray(raw)
    except Exception:
        return None


def gray_small(image: Image.Image) -> np.ndarray:
    """缩到 160x90 灰度图，专供帧差分（避免在 4K 原图上做无谓计算）。"""
    converted = image.convert("L")
    if converted.size != (GRAY_WIDTH, GRAY_HEIGHT):
        converted = converted.resize((GRAY_WIDTH, GRAY_HEIGHT))
    return np.asarray(converted, dtype=np.uint8)


def encode_jpeg(image: Image.Image, width: int = VISION_WIDTH) -> bytes:
    """缩到目标宽度并编码为 JPEG，供视觉模型消费（原图不落盘）。"""
    if image.width > width:
        ratio = width / image.width
        image = image.resize((width, max(1, int(image.height * ratio))), Image.LANCZOS)
    if image.width < width // 2:
        # 低分辨率屏幕截图放大无信息增益，保持原尺寸即可。
        pass
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
