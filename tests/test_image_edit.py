"""image_edit 层测试：镜像几何、触发词边界、素材候选链与命令接线。"""

from __future__ import annotations

import asyncio
import io
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import atri_qq_bot.onebot.server_parts.server_image_edit as server_image_edit_module
from atri_qq_bot.image_edit import (
    first_image_segment,
    is_animated,
    is_symmetry_request,
    load_segment_image_bytes,
    mirror_image,
    mirror_left_to_right,
    parse_symmetry_request,
)
from atri_qq_bot.image_edit.gif import _rgba_to_palette, write_mirrored_gif
from atri_qq_bot.onebot.server import OneBotServer


def _gradient_image(width: int, height: int = 1) -> Image.Image:
    image = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), (x * 10, 0, 0))
    return image


def _vgradient_image(height: int, width: int = 1) -> Image.Image:
    image = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (y * 10, 0, 0))
    return image


class TestMirror:
    def test_even_width_right_half_replaced_by_flipped_left(self) -> None:
        source = _gradient_image(6)

        result = mirror_left_to_right(source)

        colors = [result.getpixel((x, 0)) for x in range(6)]
        # 左半保持 0,10,20；右半 = 左半镜像 20,10,0。
        assert colors == [(0, 0, 0), (10, 0, 0), (20, 0, 0), (20, 0, 0), (10, 0, 0), (0, 0, 0)]

    def test_odd_width_center_column_stays(self) -> None:
        source = _gradient_image(5)

        result = mirror_left_to_right(source)

        colors = [result.getpixel((x, 0)) for x in range(5)]
        # 中列 x2 恰在对称轴上保持不变；x3<-x1, x4<-x0。
        assert colors == [(0, 0, 0), (10, 0, 0), (20, 0, 0), (10, 0, 0), (0, 0, 0)]

    def test_alpha_channel_is_mirrored_too(self) -> None:
        source = Image.new("RGBA", (4, 1))
        source.putpixel((0, 0), (255, 0, 0, 255))
        source.putpixel((1, 0), (0, 255, 0, 128))
        source.putpixel((2, 0), (0, 0, 255, 10))
        source.putpixel((3, 0), (0, 0, 0, 77))

        result = mirror_left_to_right(source)

        assert result.getpixel((2, 0)) == (0, 255, 0, 128)
        assert result.getpixel((3, 0)) == (255, 0, 0, 255)
        assert result.getpixel((0, 0)) == (255, 0, 0, 255)

    def test_tiny_image_returns_unchanged_copy(self) -> None:
        source = Image.new("RGB", (1, 1), (5, 5, 5))

        result = mirror_left_to_right(source)

        assert result.size == (1, 1)
        assert result.getpixel((0, 0)) == (5, 5, 5)
        assert result is not source

    def test_palette_mode_is_preserved(self) -> None:
        source = Image.new("P", (4, 1))
        source.putpixel((0, 0), 1)
        source.putpixel((1, 0), 2)
        source.putpixel((2, 0), 3)
        source.putpixel((3, 0), 4)

        result = mirror_left_to_right(source)

        assert result.mode == "P"
        assert [result.getpixel((x, 0)) for x in range(4)] == [1, 2, 2, 1]

    def test_source_image_is_not_mutated(self) -> None:
        source = _gradient_image(4)
        before = [source.getpixel((x, 0)) for x in range(4)]

        mirror_left_to_right(source)

        assert [source.getpixel((x, 0)) for x in range(4)] == before

    def test_right_direction_even_width(self) -> None:
        source = _gradient_image(6)

        result = mirror_image(source, "right")

        colors = [result.getpixel((x, 0)) for x in range(6)]
        # 右半保持 30,40,50；左半 = 右半镜像 50,40,30。
        assert colors == [
            (50, 0, 0), (40, 0, 0), (30, 0, 0), (30, 0, 0), (40, 0, 0), (50, 0, 0),
        ]

    def test_right_direction_odd_width_center_column_stays(self) -> None:
        source = _gradient_image(5)

        result = mirror_image(source, "right")

        colors = [result.getpixel((x, 0)) for x in range(5)]
        # 中列 x2 保持 20；x1<-x3, x0<-x4。
        assert colors == [(40, 0, 0), (30, 0, 0), (20, 0, 0), (30, 0, 0), (40, 0, 0)]

    def test_top_direction_even_height(self) -> None:
        source = _vgradient_image(6)

        result = mirror_image(source, "top")

        colors = [result.getpixel((0, y)) for y in range(6)]
        # 上半保持 0,10,20；下半 = 上半镜像 20,10,0。
        assert colors == [
            (0, 0, 0), (10, 0, 0), (20, 0, 0), (20, 0, 0), (10, 0, 0), (0, 0, 0),
        ]

    def test_bottom_direction_odd_height_center_row_stays(self) -> None:
        source = _vgradient_image(5)

        result = mirror_image(source, "bottom")

        colors = [result.getpixel((0, y)) for y in range(5)]
        # 中行 y2 保持 20；y1<-y3, y0<-y4。
        assert colors == [(40, 0, 0), (30, 0, 0), (20, 0, 0), (30, 0, 0), (40, 0, 0)]

    def test_palette_mode_is_preserved_for_directions(self) -> None:
        source = Image.new("P", (4, 4))
        for x in range(4):
            source.putpixel((x, 0), x + 1)

        result = mirror_image(source, "right")

        assert result.mode == "P"
        # mid=2：右半 [3,4] 镜像贴到左半 → [4,3,3,4]。
        assert [result.getpixel((x, 0)) for x in range(4)] == [4, 3, 3, 4]

    def test_tiny_dimension_returns_unchanged_copy_per_direction(self) -> None:
        wide = Image.new("RGB", (1, 4), (5, 5, 5))
        tall = Image.new("RGB", (4, 1), (6, 6, 6))

        for direction in ("left", "right"):
            assert mirror_image(wide, direction).getpixel((0, 0)) == (5, 5, 5)
        for direction in ("top", "bottom"):
            assert mirror_image(tall, direction).getpixel((0, 0)) == (6, 6, 6)

    def test_unknown_direction_raises(self) -> None:
        source = _gradient_image(4)

        with pytest.raises(ValueError):
            mirror_image(source, "diagonal")


class TestSymmetryTrigger:
    @pytest.mark.parametrize(
        "text",
        [
            "对称",
            "对称。",
            "对称一下",
            "对称处理",
            "对称处理一下",
            "镜像",
            "镜像一下",
            "帮我对称",
            "左右对称",
            "左对称",
            "左边对称",
            "亚托莉 对称",
            "亚托莉，对称呀",
            "@亚托莉 对称",
            "弄对称",
            "能不能给我对称一下",
            "对称吧",
            "对称吗？",
        ],
    )
    def test_accepts_imperative_variants(self, text: str) -> None:
        assert is_symmetry_request(text), text

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "今天天气不错",
            "不要对称",
            "别对称",
            "不用镜像",
            "先不镜像",
            "右边不要对称",
            "不要右边对称",
            "别上镜像",
            "不用下面对称",
            "垂直对称",
            "垂直镜像",
            "水平对称",
            "这个构图好对称但我说的是构图",
            "对称化设计很好",
        ],
    )
    def test_rejects_negation_ambiguity_and_chitchat(self, text: str) -> None:
        assert parse_symmetry_request(text) is None, text

    @pytest.mark.parametrize(
        "text, direction",
        [
            ("对称", "left"),
            ("镜像", "left"),
            ("左对称", "left"),
            ("左边对称", "left"),
            ("左右对称", "left"),
            ("左右镜像", "left"),
            ("右对称", "right"),
            ("右边对称", "right"),
            ("右镜像", "right"),
            ("右边镜像", "right"),
            ("上对称", "top"),
            ("上面对称", "top"),
            ("上边对称", "top"),
            ("上半对称", "top"),
            ("上镜像", "top"),
            ("上下对称", "top"),
            ("上下镜像", "top"),
            ("下对称", "bottom"),
            ("下面对称", "bottom"),
            ("下边对称", "bottom"),
            ("下半镜像", "bottom"),
            ("下镜像", "bottom"),
            ("左半对称", "left"),
            ("右半镜像", "right"),
            ("帮我对称", "left"),
            ("亚托莉，右对称呀", "right"),
            ("能不能给我上对称一下", "top"),
            ("帮我左半对称", "left"),
        ],
    )
    def test_parses_direction_from_imperative(
        self, text: str, direction: str
    ) -> None:
        assert parse_symmetry_request(text) == direction, text


class TestSegmentLookup:
    def test_first_image_segment_skips_flash_and_non_image_types(self) -> None:
        segments: list[dict[str, Any]] = [
            {"type": "image", "data": {"flash": True, "url": "https://x/flash.jpg"}},
            {"type": "mface", "data": {"url": "https://x/mface"}},
            {"type": "text", "data": {"text": "对称"}},
        ]
        assert first_image_segment(segments) is None

        target = {"type": "image", "data": {"url": "https://x/real.jpg"}}
        segments.append(target)
        assert first_image_segment(segments) is target

    def test_load_segment_image_bytes_prefers_url(self, monkeypatch) -> None:
        import atri_qq_bot.image_edit.fetch as fetch_module

        async def fake_download(url: str) -> bytes:
            assert url == "https://example.test/a.png"
            return b"png-bytes"

        monkeypatch.setattr(fetch_module, "download_image", fake_download)
        segment = {"type": "image", "data": {"url": "https://example.test/a.png"}}

        assert asyncio.run(load_segment_image_bytes(segment)) == b"png-bytes"

    def test_load_segment_image_bytes_falls_back_to_local_path(
        self, tmp_path: Path
    ) -> None:
        image_file = tmp_path / "cached.png"
        image_file.write_bytes(b"local-bytes")
        segment = {"type": "image", "data": {"file": str(image_file)}}

        assert asyncio.run(load_segment_image_bytes(segment)) == b"local-bytes"

    def test_load_segment_image_bytes_uses_get_image_action(
        self, monkeypatch
    ) -> None:
        import atri_qq_bot.image_edit.fetch as fetch_module

        async def fake_download(url: str) -> bytes:
            assert url == "https://fallback.test/b.png"
            return b"fallback-bytes"

        async def fake_action_caller(action: str, params: dict, timeout: float):
            assert action == "get_image"
            assert params == {"file": "ABC123.image"}
            return {"data": {"url": "https://fallback.test/b.png"}}

        monkeypatch.setattr(fetch_module, "download_image", fake_download)
        segment = {"type": "image", "data": {"file": "ABC123.image"}}

        result = asyncio.run(load_segment_image_bytes(segment, fake_action_caller))
        assert result == b"fallback-bytes"

    def test_load_segment_image_bytes_returns_none_when_all_fail(
        self, monkeypatch
    ) -> None:
        import atri_qq_bot.image_edit.fetch as fetch_module

        async def failing_download(url: str) -> bytes:
            raise RuntimeError("network down")

        async def failing_action_caller(action: str, params: dict, timeout: float):
            raise RuntimeError("action failed")

        monkeypatch.setattr(fetch_module, "download_image", failing_download)
        segment = {"type": "image", "data": {"url": "https://example.test/x.png", "file": "X.image"}}

        assert asyncio.run(load_segment_image_bytes(segment, failing_action_caller)) is None


def _distinct_frames(
    count: int, size: tuple[int, int] = (8, 4)
) -> list[Image.Image]:
    """逐帧唯一（颜色编码帧序号）：相同帧会被 Pillow 写入端去重合并。"""
    frames = []
    for index in range(count):
        frame = Image.new("RGBA", size, (255, 0, 0, 255))
        frame.putpixel(
            (0, 0), (index % 256, (index // 256) % 256, 255, 255)
        )
        frames.append(frame)
    return frames


def _build_gif_bytes(
    frames: list[Image.Image],
    duration: int | list[int],
    loop: int = 0,
) -> bytes:
    palette_frames = [_rgba_to_palette(frame) for frame in frames]
    durations = duration if isinstance(duration, list) else [duration] * len(frames)
    buffer = io.BytesIO()
    palette_frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        duration=durations,
        loop=loop,
        disposal=2,
        transparency=255,
        optimize=False,
    )
    return buffer.getvalue()


def _assert_left_mirrored(frame: Image.Image) -> None:
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    mid = (width + 1) // 2
    left = rgba.crop((0, 0, mid, height))
    right = rgba.crop((width - mid, 0, width, height))
    assert left.tobytes() == right.transpose(Image.FLIP_LEFT_RIGHT).tobytes()


def _assert_top_mirrored(frame: Image.Image) -> None:
    rgba = frame.convert("RGBA")
    width, height = rgba.size
    mid = (height + 1) // 2
    top = rgba.crop((0, 0, width, mid))
    bottom = rgba.crop((0, height - mid, width, height))
    assert top.tobytes() == bottom.transpose(Image.FLIP_TOP_BOTTOM).tobytes()


class _EofTruncatingSource:
    """病态 GIF 替身：n_frames 虚高，seek 越界抛 EOFError。"""

    def __init__(self, real: Image.Image, readable: int) -> None:
        self._real = real
        self._readable = readable
        self.size = real.size
        self.info = real.info
        self.n_frames = readable + 3

    def seek(self, index: int) -> None:
        if index >= self._readable:
            raise EOFError("no more images in GIF file")
        self._real.seek(index)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestGifMirror:
    def _mirror_and_open(
        self, gif_bytes: bytes, direction: str = "left"
    ) -> Image.Image:
        source = Image.open(io.BytesIO(gif_bytes))
        output = Path(tempfile.gettempdir()) / f"test_mirror_{uuid.uuid4().hex}.gif"
        try:
            write_mirrored_gif(source, output, direction=direction)
            data = output.read_bytes()
        finally:
            output.unlink(missing_ok=True)
        return Image.open(io.BytesIO(data))

    def test_every_frame_is_left_mirrored(self) -> None:
        frames = []
        for step in range(3):
            frame = Image.new("RGBA", (8, 4), (0, 0, 0, 0))
            # 每帧左半放一个不同位置的色块，右半留空——不对称的输入。
            for x in range(4):
                frame.putpixel((x, step % 4), (255, 0, 0, 255))
            frames.append(frame)

        with self._mirror_and_open(_build_gif_bytes(frames, 40)) as result:
            assert result.is_animated is True
            assert result.n_frames == 3
            for index in range(result.n_frames):
                result.seek(index)
                _assert_left_mirrored(result)

    def test_every_frame_is_top_mirrored_with_direction(self) -> None:
        frames = []
        for step in range(3):
            frame = Image.new("RGBA", (4, 8), (0, 0, 0, 0))
            # 每帧上半放一个不同位置的色块，下半留空——垂直不对称的输入。
            for y in range(4):
                frame.putpixel((step % 4, y), (0, 255, 0, 255))
            frames.append(frame)

        with self._mirror_and_open(
            _build_gif_bytes(frames, 40), direction="top"
        ) as result:
            assert result.is_animated is True
            assert result.n_frames == 3
            for index in range(result.n_frames):
                result.seek(index)
                _assert_top_mirrored(result)

    def test_delta_frame_gif_is_composited_before_mirror(self) -> None:
        # 帧2 只有 10x10 补丁（真·增量帧）。输出帧2 必须是完整画布上
        # 按 disposal=2 语义合成（帧1 播完清回透明背景）后再镜像的结果，
        # 而不是把 10x10 补丁单独镜像。
        full = Image.new("RGBA", (100, 80), (255, 0, 0, 255))
        patch = Image.new("RGBA", (10, 10), (0, 0, 255, 255))

        with self._mirror_and_open(
            _build_gif_bytes([full, patch], 40)
        ) as result:
            assert result.n_frames == 2
            result.seek(1)
            assert result.size == (100, 80)
            rgba = result.convert("RGBA")
            # 补丁在左上角，镜像后落在右上角；其余画布按 disposal=2
            # 语义清为透明背景。
            assert rgba.getpixel((5, 5))[:3] == (0, 0, 255)
            assert rgba.getpixel((95, 5))[:3] == (0, 0, 255)
            assert rgba.getpixel((50, 40)) == (0, 0, 0, 0)

    def test_transparency_survives_roundtrip(self) -> None:
        frame = Image.new("RGBA", (8, 4), (0, 0, 0, 0))
        frame.putpixel((1, 1), (255, 0, 0, 255))

        with self._mirror_and_open(_build_gif_bytes([frame], 40)) as result:
            result.seek(0)
            rgba = result.convert("RGBA")
            assert rgba.getpixel((0, 0))[3] == 0
            assert rgba.getpixel((7, 0))[3] == 0
            assert rgba.getpixel((1, 1))[:3] == (255, 0, 0)
            assert rgba.getpixel((6, 1))[:3] == (255, 0, 0)

    def test_zero_duration_is_normalized(self) -> None:
        frames = _distinct_frames(2)

        with self._mirror_and_open(
            _build_gif_bytes(frames, [0, 50])
        ) as result:
            assert result.n_frames == 2
            result.seek(0)
            assert result.info.get("duration") == 80
            result.seek(1)
            assert result.info.get("duration") == 50

    def test_loop_count_is_preserved(self) -> None:
        frames = _distinct_frames(2)

        with self._mirror_and_open(
            _build_gif_bytes(frames, 40, loop=3)
        ) as result:
            assert result.info.get("loop") == 3

    def test_frame_cap_sampling_preserves_total_duration(self) -> None:
        frames = _distinct_frames(350)

        with self._mirror_and_open(_build_gif_bytes(frames, 20)) as result:
            assert result.n_frames == 175
            total = 0
            for index in range(result.n_frames):
                result.seek(index)
                _assert_left_mirrored(result)
                total += int(result.info.get("duration") or 0)
            # 350 帧 × 20ms = 7000ms，采样后时长守恒（GIF 10ms 粒度）。
            assert total == 7000

    def test_huge_gif_is_downscaled_to_budget(self) -> None:
        frames = _distinct_frames(1, size=(1500, 40))

        with self._mirror_and_open(_build_gif_bytes(frames, 40)) as result:
            result.seek(0)
            assert result.size[0] <= 1024
            _assert_left_mirrored(result)

    def test_is_animated_detects_only_multi_frame(self) -> None:
        assert is_animated(Image.new("RGB", (4, 4))) is False
        gif_bytes = _build_gif_bytes(_distinct_frames(2), 40)
        with Image.open(io.BytesIO(gif_bytes)) as animated:
            assert is_animated(animated) is True

    def test_truncated_gif_emits_decoded_frames_only(self) -> None:
        # n_frames 虚高（报 5 实有 2）：输出必须只含成功解码的帧，
        # 每一帧都对称，绝不发出半残帧。
        gif_bytes = _build_gif_bytes(_distinct_frames(3), 40)
        with Image.open(io.BytesIO(gif_bytes)) as real:
            source = _EofTruncatingSource(real, readable=2)
            output = Path(tempfile.gettempdir()) / f"test_mirror_{uuid.uuid4().hex}.gif"
            try:
                written = write_mirrored_gif(source, output)
                data = output.read_bytes()
            finally:
                output.unlink(missing_ok=True)

        assert written == 2
        with Image.open(io.BytesIO(data)) as result:
            assert result.n_frames == 2
            for index in range(result.n_frames):
                result.seek(index)
                _assert_left_mirrored(result)


class TestStaticWrite:
    def test_huge_static_image_is_capped_and_mirrored(
        self, tmp_path: Path
    ) -> None:
        source = Image.new("RGB", (5000, 1))
        for x in range(5000):
            source.putpixel((x, 0), (x % 256, 0, 0))
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")

        output = tmp_path / "capped.png"
        server_image_edit_module._write_mirrored_static(
            buffer.getvalue(), output, "left"
        )

        with Image.open(output) as result:
            assert result.size == (4096, 1)
            _assert_left_mirrored(result)


def _server_stub() -> OneBotServer:
    server = object.__new__(OneBotServer)
    server._mark_smart_group_reply = lambda *args, **kwargs: None  # type: ignore[method-assign]
    server._delivery_model_name = lambda kind: "test-model"  # type: ignore[method-assign]
    server._schedule_image_edit_cleanup = lambda path, delay=60.0: None  # type: ignore[method-assign]
    server.reply_engine = SimpleNamespace(record_bot_reply=lambda *a, **k: None)
    return server


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestSymmetryCommand:
    def _patch_sending(self, server: OneBotServer, sent: list) -> None:
        async def fake_send_reply(websocket, event, outgoing, **kwargs):
            sent.append(list(outgoing))
            return "[图片]"

        server.send_reply = fake_send_reply  # type: ignore[method-assign]

    def test_mirrors_quoted_image_end_to_end(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        source = Image.new("RGB", (6, 2))
        for y in range(2):
            for x in range(6):
                source.putpixel((x, y), (200, 0, 0) if x < 3 else (0, 0, 200))
        png = _png_bytes(source)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.png"}}],
            )

        async def fake_load(segment, action_caller=None):
            return png

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "image"
        output_path = Path(sent[0][0].content)
        try:
            with Image.open(output_path) as result:
                assert result.size == (6, 2)
                # 右半被左半镜像覆盖：全图变红。
                assert result.getpixel((4, 0)) == (200, 0, 0)
                assert result.getpixel((5, 1)) == (200, 0, 0)
        finally:
            output_path.unlink(missing_ok=True)

    def test_mirrors_quoted_image_with_direction_end_to_end(
        self, monkeypatch
    ) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        # 2x6 竖图：上半红、下半蓝。命令「下对称」= 下半镜像覆盖上半。
        source = Image.new("RGB", (2, 6))
        for y in range(6):
            for x in range(2):
                source.putpixel((x, y), (200, 0, 0) if y < 3 else (0, 0, 200))
        png = _png_bytes(source)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.png"}}],
            )

        async def fake_load(segment, action_caller=None):
            return png

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "下对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "下对称", "private:10001"
            )
        )

        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "image"
        output_path = Path(sent[0][0].content)
        try:
            with Image.open(output_path) as result:
                assert result.size == (2, 6)
                # 上半被下半镜像覆盖：全图变蓝。
                assert result.getpixel((0, 0)) == (0, 0, 200)
                assert result.getpixel((1, 2)) == (0, 0, 200)
                _assert_top_mirrored(result)
        finally:
            output_path.unlink(missing_ok=True)

    def test_exif_oriented_photo_is_upright_before_mirror(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        # 4x2 横图 + orientation=6（显示时旋转 90°）：先摆正成 2x4 再镜像，
        # 否则用户看到的对称轴是被旋转过的。
        source = Image.new("RGB", (4, 2), (120, 120, 120))
        exif = Image.Exif()
        exif[0x0112] = 6
        buffer = io.BytesIO()
        source.save(buffer, format="JPEG", exif=exif)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.jpg"}}],
            )

        async def fake_load(segment, action_caller=None):
            return buffer.getvalue()

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        output_path = Path(sent[0][0].content)
        try:
            with Image.open(output_path) as result:
                assert result.size == (2, 4)
        finally:
            output_path.unlink(missing_ok=True)

    def test_uses_current_message_image_when_no_quote(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        png = _png_bytes(_gradient_image(4))

        async def fake_resolve(websocket, event):
            return "", "", []

        async def fake_load(segment, action_caller=None):
            return png

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "image", "data": {"url": "https://example.test/cur.png"}},
                {"type": "text", "data": {"text": "镜像一下"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "镜像一下", "private:10001"
            )
        )

        assert handled is True
        assert sent[0][0].kind == "image"
        Path(sent[0][0].content).unlink(missing_ok=True)

    def test_passes_through_when_no_image_anywhere(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        async def fake_resolve(websocket, event):
            return "1001", "被引用的文字", [{"type": "text", "data": {"text": "文字"}}]

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)

        event = {
            "message_type": "group",
            "message": [{"type": "text", "data": {"text": "这构图好对称"}}],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "这构图好对称", "group:1"
            )
        )

        # 闲聊误拦是红线：没有图片就放行给聊天模型。
        assert handled is False
        assert sent == []

    def test_reports_honest_failure_when_download_fails(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.png"}}],
            )

        async def fake_load(segment, action_caller=None):
            return None

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "text"
        assert sent[0][0].content == "这张图我拿不下来，稍后再试试"

    def test_reports_honest_failure_on_undecodable_bytes(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.png"}}],
            )

        async def fake_load(segment, action_caller=None):
            return b"not-an-image"

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert sent[0][0].content == "这张图我打不开呀，格式好像不对"

    def test_probe_exception_of_any_kind_never_leaks(self, monkeypatch) -> None:
        # Pillow 对损坏文件抛什么异常没有稳定契约（截断 GIF 的
        # n_frames 扫描抛 IndexError，而非 EOFError）：探测层必须对
        # 一切异常坦诚降级，绝不穿透到事件层。
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        def broken_probe(image):
            raise IndexError("index out of range")

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/bad.gif"}}],
            )

        async def fake_load(segment, action_caller=None):
            return _build_gif_bytes(_distinct_frames(2), 40)

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )
        monkeypatch.setattr(server_image_edit_module, "is_animated", broken_probe)

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "text"
        assert sent[0][0].content == "这张图我打不开呀，格式好像不对"

    def test_truncated_gif_bytes_never_crash_the_pipeline(self, monkeypatch) -> None:
        # 真实世界的截断动图（下载中断/存储损坏）：整条链路必须以
        # 坦诚文本收场，绝不把异常抛给事件层。
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        full_gif = _build_gif_bytes(_distinct_frames(4), 40)
        truncated = full_gif[: int(len(full_gif) * 0.8)]

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/cut.gif"}}],
            )

        async def fake_load(segment, action_caller=None):
            return truncated

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "text"

    def test_non_symmetry_text_never_touches_quoted_lookup(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        async def unexpected_resolve(websocket, event):
            raise AssertionError("non-symmetry text must not resolve quotes")

        monkeypatch.setattr(server, "_resolve_quoted_message", unexpected_resolve)

        event = {"message_type": "private", "message": []}
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "今天天气不错", "private:10001"
            )
        )

        assert handled is False
        assert sent == []

    def _build_gif_bytes(
        self,
        frames: list[Image.Image],
        duration: int | list[int],
        loop: int = 0,
    ) -> bytes:
        palette_frames = [_rgba_to_palette(frame) for frame in frames]
        durations = duration if isinstance(duration, list) else [duration] * len(frames)
        buffer = io.BytesIO()
        palette_frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=palette_frames[1:],
            duration=durations,
            loop=loop,
            disposal=2,
            transparency=255,
            optimize=False,
        )
        return buffer.getvalue()

    def test_mirrors_quoted_gif_end_to_end(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []

        # 真实 send_reply 对 image 消息恒返回空串（只有 text/record 进
        # delivered_parts）——空串是正常契约，不是失败信号。
        async def fake_send_reply(websocket, event, outgoing, **kwargs):
            sent.append(list(outgoing))
            return ""

        server.send_reply = fake_send_reply  # type: ignore[method-assign]

        frames = []
        for step in range(3):
            frame = Image.new("RGBA", (8, 4), (0, 0, 0, 0))
            for x in range(4):
                frame.putpixel((x, step % 4), (255, 0, 0, 255))
            frames.append(frame)
        gif_bytes = _build_gif_bytes(frames, 40)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/a.gif"}}],
            )

        async def fake_load(segment, action_caller=None):
            return gif_bytes

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert sent[0][0].kind == "image"
        output_path = Path(sent[0][0].content)
        try:
            assert output_path.suffix == ".gif"
            with Image.open(output_path) as result:
                assert result.is_animated is True
                assert result.n_frames == 3
                for index in range(result.n_frames):
                    result.seek(index)
                    _assert_left_mirrored(result)
        finally:
            output_path.unlink(missing_ok=True)

    def test_gif_timeout_reports_honest_failure(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)
        cleanup_delays: list[float] = []
        monkeypatch.setattr(
            server,
            "_schedule_image_edit_cleanup",
            lambda path, delay=60.0: cleanup_delays.append(delay),
        )

        gif_bytes = _build_gif_bytes(_distinct_frames(2), 40)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/a.gif"}}],
            )

        async def fake_load(segment, action_caller=None):
            return gif_bytes

        def slow_write(source, output_path, **kwargs):
            time.sleep(0.3)
            raise AssertionError("timeout must abort before write completes")

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )
        monkeypatch.setattr(server_image_edit_module, "write_mirrored_gif", slow_write)
        monkeypatch.setattr(
            server_image_edit_module, "_GIF_EDIT_TIMEOUT_SECONDS", 0.05
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        # 超时线程仍在后台跑：给足时间再断言，避免 Windows 上 executor
        # 未结算的 sleep 线程影响后续用例。
        time.sleep(0.35)
        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "text"
        assert sent[0][0].content == "这张动图我处理不动呀，稍后再试试"
        # 超时后线程仍可能继续写文件：清理延迟必须盖过 90s 处理窗口。
        assert cleanup_delays == [180.0]

    def test_static_timeout_reports_honest_failure(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)
        cleanup_delays: list[float] = []
        monkeypatch.setattr(
            server,
            "_schedule_image_edit_cleanup",
            lambda path, delay=60.0: cleanup_delays.append(delay),
        )

        png = _png_bytes(_gradient_image(4))

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/img.png"}}],
            )

        async def fake_load(segment, action_caller=None):
            return png

        def slow_write(image_bytes, output_path):
            time.sleep(0.3)
            raise AssertionError("timeout must abort before write completes")

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )
        monkeypatch.setattr(
            server_image_edit_module, "_write_mirrored_static", slow_write
        )
        monkeypatch.setattr(
            server_image_edit_module, "_STATIC_EDIT_TIMEOUT_SECONDS", 0.05
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        time.sleep(0.35)
        assert handled is True
        assert len(sent) == 1
        assert sent[0][0].kind == "text"
        assert sent[0][0].content == "这张图我处理不动呀，稍后再试试"
        assert cleanup_delays == [60.0]

    def test_gif_write_error_reports_honest_failure(self, monkeypatch) -> None:
        server = _server_stub()
        sent: list = []
        self._patch_sending(server, sent)

        gif_bytes = _build_gif_bytes(_distinct_frames(2), 40)

        async def fake_resolve(websocket, event):
            return (
                "1001",
                "",
                [{"type": "image", "data": {"url": "https://example.test/a.gif"}}],
            )

        async def fake_load(segment, action_caller=None):
            return gif_bytes

        def failing_write(source, output_path, **kwargs):
            raise ValueError("mirrored gif exceeds size budget")

        monkeypatch.setattr(server, "_resolve_quoted_message", fake_resolve)
        monkeypatch.setattr(
            server_image_edit_module, "load_segment_image_bytes", fake_load
        )
        monkeypatch.setattr(
            server_image_edit_module, "write_mirrored_gif", failing_write
        )

        event = {
            "message_type": "private",
            "message": [
                {"type": "reply", "data": {"id": "1001"}},
                {"type": "text", "data": {"text": "对称"}},
            ],
        }
        handled = asyncio.run(
            server._maybe_handle_image_edit_command(
                object(), event, "对称", "private:10001"
            )
        )

        assert handled is True
        assert sent[0][0].kind == "text"
        assert sent[0][0].content == "这张动图我处理不动呀，稍后再试试"
