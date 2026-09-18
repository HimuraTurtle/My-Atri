from __future__ import annotations

from pathlib import Path
from typing import Any

from .schema import OutgoingMessage


def outgoing_to_onebot_message(message: OutgoingMessage) -> str | list[dict[str, Any]]:
    if message.kind == "text":
        return message.content
    if message.kind == "face":
        face_id: int | str = int(message.content) if message.content.isdigit() else message.content
        return [{"type": "face", "data": {"id": face_id}}]
    if message.kind == "record":
        return [{"type": "record", "data": {"file": message.content, "cache": 0}}]
    if message.kind == "file":
        # NapCat 对 send_msg 的 file 段支持不稳定；正式发送由
        # _send_one_reply 走 upload_group_file / upload_private_file。
        return [
            {
                "type": "file",
                "data": {"file": message.content, "name": Path(message.content).name},
            }
        ]
    if message.kind == "sticker":
        return [
            {
                "type": "image",
                "data": {
                    "file": message.content,
                    "cache": 0,
                    "sub_type": message.sub_type or 1,
                    "summary": message.summary or "[动画表情]",
                },
            }
        ]
    return [{"type": "image", "data": {"file": message.content, "cache": 0}}]
