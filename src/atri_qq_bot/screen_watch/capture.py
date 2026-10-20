"""屏幕截图：一次采集派生两种用途（差分小图 + 视觉 JPEG）。

GDI 抓屏（mss）在独显全屏游戏、模式切换瞬间会瞬时失败：
先原地重试，仍失败退到 PIL ImageGrab（另一条 GDI 实现路径）兜底，
全部失败才返回 None 并记录最近一次异常——绝不静默吞掉错误细节。
"""

from __future__ import annotations

import io
import logging
import time

import numpy as np

try:
    import mss
except Exception:  # pragma: no cover - 采集依赖缺失时上层按失败降级
    mss = None

from PIL import Image

LOGGER = logging.getLogger("atri.screen_watch")

GRAY_WIDTH = 160
GRAY_HEIGHT = 90
VISION_WIDTH = 1280
JPEG_QUALITY = 82

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 0.2


def _grab_via_mss(monitor: int) -> Image.Image:
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[monitor])
    raw = np.asarray(shot, dtype=np.uint8)[..., :3]
    return Image.fromarray(raw)


def _grab_via_imagegrab(monitor: int) -> Image.Image:
    # ImageGrab 默认抓主屏，与 mss.monitors[1] 语义一致；多显示器不在
    # 本功能的采集范围内。
    from PIL import ImageGrab

    return ImageGrab.grab()


def capture_screen(monitor: int = 1) -> Image.Image | None:
    """抓取指定显示器全屏，返回 RGB PIL Image；失败返回 None。"""
    if mss is None:
        return None
    last_error: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS + 1):
        try:
            return _grab_via_mss(monitor)
        except Exception as exc:
            last_error = exc
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    try:
        return _grab_via_imagegrab(monitor)
    except Exception as exc:
        last_error = exc
    LOGGER.warning(
        "屏幕截图失败（重试 %d 次 + ImageGrab 兜底均失败）：%s: %s",
        _RETRY_ATTEMPTS,
        type(last_error).__name__ if last_error else "UnknownError",
        last_error,
    )
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
