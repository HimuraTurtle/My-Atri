"""GIF 对称修图：逐帧完整合成 → 逐帧镜像 → 全帧重写回 GIF。

第一性原理：GIF 帧间是增量编码（每帧只存变化区域），「每一帧都对称」
的正确路径是把每一帧完整合成出来再镜像——对增量帧直接做左半替换会把
上一帧的残影镜像进结果。输出端统一写全帧（disposal=2 帧帧替换），
从根上杜绝输出 GIF 再依赖增量合成。

资源契约（对抗病态 GIF）：
- 单帧边长超过 ``max_dimension`` 先降采样（镜像不需要超清保真）；
- 帧数超过 ``max_frames`` 按步长采样，被跳帧的时长累到保留帧上，
  总播放时长守恒；
- 输出超过 ``max_bytes`` 时用 640px 重编码一次，仍超预算则抛
  ValueError 交给上层坦诚降级。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from PIL import Image

from .mirror import mirror_image

GIF_MAX_DIMENSION = 1024
GIF_RESCALE_DIMENSION = 640
GIF_MAX_FRAMES = 300
GIF_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
DEFAULT_FRAME_MS = 80

_TRANSPARENT_INDEX = 255
_ALPHA_THRESHOLD = 128


def is_animated(image: Image.Image) -> bool:
    return bool(getattr(image, "is_animated", False)) and getattr(image, "n_frames", 1) > 1


def _normalized_duration(source: Image.Image) -> int:
    raw = int(source.info.get("duration") or 0)
    return raw if raw > 0 else DEFAULT_FRAME_MS


def _sampling_step(frame_count: int, max_frames: int) -> int:
    if frame_count <= max_frames:
        return 1
    return -(-frame_count // max_frames)


def _target_size(size: tuple[int, int], max_dimension: int) -> tuple[int, int] | None:
    width, height = size
    longest = max(width, height)
    if longest <= max_dimension:
        return None
    scale = max_dimension / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _rgba_to_palette(frame: Image.Image) -> Image.Image:
    """RGBA → 带 255 号透明索引的 P 帧（255 色调色板留出透明槽位）。"""
    alpha = frame.getchannel("A")
    mask = alpha.point(lambda a: 255 if a <= _ALPHA_THRESHOLD else 0)
    palette_frame = frame.convert("RGB").convert(
        "P", palette=Image.ADAPTIVE, colors=_TRANSPARENT_INDEX
    )
    palette_frame.paste(_TRANSPARENT_INDEX, mask)
    return palette_frame


def _mirror_frame(
    source: Image.Image,
    index: int,
    target_size: tuple[int, int] | None,
    direction: str,
) -> Image.Image:
    source.seek(index)
    frame = source.convert("RGBA")
    if target_size is not None:
        frame = frame.resize(target_size, Image.LANCZOS)
    return _rgba_to_palette(mirror_image(frame, direction))


def _save_mirrored_gif(
    source: Image.Image,
    output_path: Path,
    durations: list[int],
    indices: range,
    loop: int,
    target_size: tuple[int, int] | None,
    direction: str,
) -> None:
    def frames() -> Iterator[Image.Image]:
        # 损坏 GIF 的 n_frames 可能虚高：seek 越界抛 EOFError 时在已
        # 解码的帧处截断，绝不发出半残帧。
        for index in indices:
            try:
                yield _mirror_frame(source, index, target_size, direction)
            except EOFError:
                return

    stream = frames()
    first = next(stream)
    first.save(
        output_path,
        format="GIF",
        save_all=True,
        append_images=stream,
        duration=durations,
        loop=loop,
        disposal=2,
        transparency=_TRANSPARENT_INDEX,
        optimize=False,
    )


def write_mirrored_gif(
    source: Image.Image,
    output_path: Path,
    max_dimension: int = GIF_MAX_DIMENSION,
    max_frames: int = GIF_MAX_FRAMES,
    max_bytes: int = GIF_MAX_OUTPUT_BYTES,
    direction: str = "left",
) -> int:
    """把动图逐帧按方向镜像后写成 GIF，返回输出的帧数。

    要求 ``source`` 是动图（is_animated 为真）；调用方负责静态图分流。
    输出超过 ``max_bytes`` 时按 640px 降采样重编码一次，仍超预算抛
    ValueError（上层坦诚降级，不发出半残结果）。
    """
    frame_count = int(getattr(source, "n_frames", 1) or 1)

    raw_durations: list[int] = []
    loop = 0
    for index in range(frame_count):
        try:
            source.seek(index)
        except EOFError:
            break
        if index == 0:
            loop = int(source.info.get("loop") or 0)
        raw_durations.append(_normalized_duration(source))
    if not raw_durations:
        raise ValueError("gif has no decodable frames")

    decoded = len(raw_durations)
    step = _sampling_step(decoded, max_frames)
    indices = range(0, decoded, step)
    durations = [sum(raw_durations[i : i + step]) for i in indices]

    _save_mirrored_gif(
        source,
        output_path,
        durations,
        indices,
        loop,
        _target_size(source.size, max_dimension),
        direction,
    )
    if output_path.stat().st_size <= max_bytes:
        return len(durations)

    _save_mirrored_gif(
        source,
        output_path,
        durations,
        indices,
        loop,
        _target_size(source.size, GIF_RESCALE_DIMENSION),
        direction,
    )
    if output_path.stat().st_size > max_bytes:
        raise ValueError("mirrored gif exceeds size budget")
    return len(durations)
