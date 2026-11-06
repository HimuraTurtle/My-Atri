"""OneBot 层的修图指令：引用图片 + 「（上/下/左/右）对称」→ 半区镜像覆盖。

确定性入口，与漫画命令同级（回复门控之后、聊天模型之前）：
- 触发条件是「对称/镜像祈使（可带方向词）+ 消息里有可用图片」的合取
  ——没有图片时放行给聊天模型，避免「这构图好对称」这类闲聊被误拦；
- 图片优先取被引用消息，其次取当前消息；
- 下载/解码/处理失败时坦诚说明，绝不编造结果。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import uuid
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from PIL import Image, ImageOps

from ...image_edit import (
    first_image_segment,
    is_animated,
    load_segment_image_bytes,
    mirror_image,
    parse_symmetry_request,
    write_mirrored_gif,
)
from ...message_plan import OutgoingMessage
from ..message_batch import _message_to_segments
from .server_support import OneBotServerMixin

LOGGER = logging.getLogger("atri.onebot")

_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_STATIC_EDIT_TIMEOUT_SECONDS = 30.0
_GIF_EDIT_TIMEOUT_SECONDS = 90.0
_STATIC_MAX_DIMENSION = 4096
_OUTPUT_DIR = Path(gettempdir()) / "atri_image_edit"

_FAILURE_DOWNLOAD = "这张图我拿不下来，稍后再试试"
_FAILURE_DECODE = "这张图我打不开呀，格式好像不对"
_FAILURE_PROCESS = "这张图我处理不动呀，稍后再试试"
_FAILURE_GIF = "这张动图我处理不动呀，稍后再试试"


def _write_mirrored_static(
    image_bytes: bytes, output_path: Path, direction: str
) -> None:
    """静态图处理（线程池执行）：EXIF 摆正 → 长边 4096 封顶 → 镜像 → PNG。"""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        longest = max(width, height)
        if longest > _STATIC_MAX_DIMENSION:
            scale = _STATIC_MAX_DIMENSION / longest
            image = image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )
        mirror_image(image, direction).save(output_path, format="PNG")


def _write_mirrored_gif(
    image_bytes: bytes, output_path: Path, direction: str
) -> int:
    """动图处理（线程池执行）：逐帧按方向镜像写回 GIF，返回输出帧数。"""
    with Image.open(io.BytesIO(image_bytes)) as source:
        return write_mirrored_gif(source, output_path, direction=direction)


class OneBotImageEditMixin(OneBotServerMixin):
    async def _maybe_handle_image_edit_command(
        self,
        websocket: Any,
        event: dict[str, Any],
        plain_text: str,
        conversation_id: str,
    ) -> bool:
        """命中「对称修图」时完成处理并返回 True，调用方直接结束本轮。"""
        direction = parse_symmetry_request(plain_text)
        if direction is None:
            return False

        segment = await self._find_edit_target_segment(websocket, event)
        if segment is None:
            # 没有任何可用图片：大概率是闲聊（「这构图好对称」），
            # 放行给聊天模型，不在这里抢话。
            return False

        handled = await self._mirror_and_reply(websocket, event, segment, direction)
        if handled:
            self._mark_smart_group_reply(event, conversation_id, plain_text)
            self.reply_engine.record_bot_reply(
                conversation_id, "[对称修图]", True
            )
        return handled

    async def _find_edit_target_segment(
        self,
        websocket: Any,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """定位要处理的图片段：被引用消息优先，其次当前消息。"""
        try:
            _message_id, _quoted_text, quoted_segments = (
                await self._resolve_quoted_message(websocket, event)
            )
        except Exception as exc:
            LOGGER.warning("修图指令读取引用消息失败：%s", exc)
            quoted_segments = []

        segment = first_image_segment(quoted_segments or [])
        if segment is not None:
            return segment
        return first_image_segment(_message_to_segments(event.get("message")))

    async def _mirror_and_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        segment: dict[str, Any],
        direction: str,
    ) -> bool:
        data = segment.get("data") or {}
        label = str(data.get("url") or data.get("file") or "")[:100]
        try:
            image_bytes = await asyncio.wait_for(
                load_segment_image_bytes(
                    segment,
                    lambda action, params, timeout: self.call_action_and_wait(
                        websocket, action, params, timeout
                    ),
                ),
                timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("对称修图取图超时：%s", label)
            image_bytes = None
        except Exception as exc:
            LOGGER.warning("对称修图取图失败：%s error=%s", label, exc)
            image_bytes = None

        if image_bytes is None:
            await self._send_image_edit_text(websocket, event, _FAILURE_DOWNLOAD)
            return True

        # 主线程只做头部探测（懒加载，不解码像素）：分流动图/静态，
        # 决定输出扩展名后再把全部像素处理丢进线程池。Pillow 对损坏
        # 文件抛什么异常没有稳定契约（EOFError/IndexError/struct.error
        # 都出现过），探测失败一律按解码失败坦诚降级，绝不穿透到事件层。
        try:
            with Image.open(io.BytesIO(image_bytes)) as probe:
                animated = is_animated(probe)
        except Exception as exc:
            LOGGER.warning("对称修图解码失败：%s", exc)
            await self._send_image_edit_text(websocket, event, _FAILURE_DECODE)
            return True

        if animated:
            return await self._mirror_gif_and_reply(
                websocket, event, image_bytes, direction
            )
        return await self._mirror_static_and_reply(
            websocket, event, image_bytes, direction
        )

    async def _mirror_static_and_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        image_bytes: bytes,
        direction: str,
    ) -> bool:
        """静态路径：EXIF 摆正 + 4096 封顶 + 镜像，全程线程池执行。"""
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _OUTPUT_DIR / f"mirror_{uuid.uuid4().hex}.png"
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(
                    None, _write_mirrored_static, image_bytes, output_path, direction
                ),
                timeout=_STATIC_EDIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("对称修图静态处理超时（%ss）", _STATIC_EDIT_TIMEOUT_SECONDS)
            self._schedule_image_edit_cleanup(output_path)
            await self._send_image_edit_text(websocket, event, _FAILURE_PROCESS)
            return True
        except Exception as exc:
            LOGGER.warning("对称修图静态处理失败：%s", exc)
            self._schedule_image_edit_cleanup(output_path)
            await self._send_image_edit_text(websocket, event, _FAILURE_PROCESS)
            return True

        LOGGER.info("对称修图完成：%s", output_path.name)
        return await self._send_edit_result(websocket, event, output_path)

    async def _mirror_gif_and_reply(
        self,
        websocket: Any,
        event: dict[str, Any],
        image_bytes: bytes,
        direction: str,
    ) -> bool:
        """动图路径：逐帧镜像后以 GIF 回发；编解码全程在线程池执行。"""
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _OUTPUT_DIR / f"mirror_{uuid.uuid4().hex}.gif"
        loop = asyncio.get_running_loop()
        try:
            frame_count = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _write_mirrored_gif, image_bytes, output_path, direction
                ),
                timeout=_GIF_EDIT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOGGER.warning("对称修图 GIF 处理超时（%ss）", _GIF_EDIT_TIMEOUT_SECONDS)
            # 超时后线程仍在写文件：清理延迟必须盖过处理超时。
            self._schedule_image_edit_cleanup(output_path, delay=180.0)
            await self._send_image_edit_text(websocket, event, _FAILURE_GIF)
            return True
        except Exception as exc:
            LOGGER.warning("对称修图 GIF 处理失败：%s", exc)
            self._schedule_image_edit_cleanup(output_path, delay=180.0)
            await self._send_image_edit_text(websocket, event, _FAILURE_GIF)
            return True

        LOGGER.info("对称修图完成（GIF %d 帧）：%s", frame_count, output_path.name)
        return await self._send_edit_result(websocket, event, output_path)

    async def _send_edit_result(
        self, websocket: Any, event: dict[str, Any], output_path: Path
    ) -> bool:
        # send_reply 对 image 消息恒返回空串（只有 text/record 进
        # delivered_parts），不能当失败信号——真实失败由 send_reply
        # 内部记 ERROR 日志与投递事件，与 anima 图片投递同一契约。
        await self.send_reply(
            websocket,
            event,
            [OutgoingMessage("image", str(output_path))],
            model_type="chat",
            model_name=self._delivery_model_name("chat"),
        )
        self._schedule_image_edit_cleanup(output_path)
        return True

    def _schedule_image_edit_cleanup(self, path: Path, delay: float = 60.0) -> None:
        """延迟清理结果文件：NapCat 读取文件是异步的，不能立刻删除。"""

        async def _remove() -> None:
            await asyncio.sleep(delay)
            with contextlib.suppress(Exception):
                path.unlink(missing_ok=True)

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(_remove())

    async def _send_image_edit_text(
        self, websocket: Any, event: dict[str, Any], text: str
    ) -> None:
        try:
            await self.send_reply(
                websocket,
                event,
                [OutgoingMessage("text", text)],
                model_type="chat",
                model_name=self._delivery_model_name("chat"),
            )
        except Exception:
            LOGGER.exception("修图失败提示发送失败")
