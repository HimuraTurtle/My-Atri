"""Milky 协议 <-> OneBot v11 的纯函数翻译层。

Milky 是 OneBot v11 的继任协议（由 Lagrange.Milky 实现），通信模型为：
- 协议端开启 HTTP 服务，``/api/:api`` 提供动作调用、``/event`` 提供事件推送；
- 应用端（ATRI）作为客户端连接 ``/event`` 的 WebSocket 收事件，
  并用 HTTP POST ``/api/:api`` 发动作。

ATRI 内部仍是 OneBot v11 事件/动作模型，本模块只做双向翻译，不持有
网络连接、不产生副作用，便于单元测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ---- 消息 ID 编解码 ----
# Milky 用 (message_scene, peer_id, message_seq) 定位消息，OneBot v11 用
# 字符串 message_id。这里合成一个可逆的字符串 ID，供 get_msg 往返。
_ID_PREFIX = "milky"


def encode_message_id(scene: str, peer_id: int, seq: int) -> str:
    return f"{_ID_PREFIX}:{scene}:{peer_id}:{seq}"


def decode_message_id(message_id: str) -> tuple[str, int, int] | None:
    parts = str(message_id or "").split(":")
    if len(parts) != 4 or parts[0] != _ID_PREFIX:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None


# ---- 文件路径 -> file:// URI ----
def to_file_uri(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith(("file://", "http://", "https://", "base64://")):
        return raw
    try:
        return Path(raw).absolute().as_uri()
    except (OSError, ValueError):
        return raw


def _data(seg: Any) -> dict[str, Any]:
    data = seg.get("data") if isinstance(seg, dict) else None
    return data if isinstance(data, dict) else {}


def segment_milky_to_onebot(seg: Any, scene: str, peer_id: int) -> dict[str, Any] | None:
    """Milky 入站消息段 -> OneBot 消息段。"""
    if not isinstance(seg, dict):
        return None
    kind = str(seg.get("type") or "").lower()
    data = _data(seg)
    if kind == "text":
        return {"type": "text", "data": {"text": str(data.get("text") or "")}}
    if kind == "mention":
        return {"type": "at", "data": {"qq": str(data.get("user_id") or "")}}
    if kind == "mention_all":
        return {"type": "at", "data": {"qq": "all"}}
    if kind == "face":
        return {"type": "face", "data": {"id": str(data.get("face_id") or "")}}
    if kind == "reply":
        return {
            "type": "reply",
            "data": {"id": encode_message_id(scene, peer_id, int(data.get("message_seq") or 0))},
        }
    if kind == "image":
        return {
            "type": "image",
            "data": {
                "file": str(data.get("resource_id") or ""),
                "url": str(data.get("temp_url") or ""),
                "sub_type": str(data.get("sub_type") or "normal"),
                "summary": str(data.get("summary") or ""),
            },
        }
    if kind == "record":
        return {"type": "record", "data": {"file": str(data.get("temp_url") or ""), "url": str(data.get("temp_url") or "")}}
    if kind == "video":
        return {"type": "video", "data": {"file": str(data.get("temp_url") or ""), "url": str(data.get("temp_url") or "")}}
    if kind == "market_face":
        return {"type": "mface", "data": {"summary": str(data.get("summary") or ""), "url": str(data.get("url") or "")}}
    if kind == "light_app":
        return {"type": "json", "data": {"data": str(data.get("json_payload") or "")}}
    # 其余类型（file/forward/xml/markdown）原样透传，ATRI 侧按需处理。
    return {"type": kind, "data": dict(data)}


def message_milky_to_onebot(message: dict[str, Any], scene: str, peer_id: int) -> list[dict[str, Any]]:
    segments = message.get("segments") if isinstance(message, dict) else None
    if not isinstance(segments, list):
        return []
    result: list[dict[str, Any]] = []
    for seg in segments:
        translated = segment_milky_to_onebot(seg, scene, peer_id)
        if translated is not None:
            result.append(translated)
    return result


def event_milky_to_onebot(event: dict[str, Any]) -> dict[str, Any] | None:
    """Milky 事件 -> OneBot 事件；无法翻译的事件返回 None（忽略）。"""
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("event_type") or "").lower()
    self_id = int(event.get("self_id") or 0)
    data = event.get("data") if isinstance(event.get("data"), dict) else {}

    if event_type == "message_receive":
        scene = str(data.get("message_scene") or "").lower()
        peer_id = int(data.get("peer_id") or 0)
        sender_id = int(data.get("sender_id") or 0)
        seq = int(data.get("message_seq") or 0)
        message = message_milky_to_onebot(data, scene, peer_id)
        if scene == "group":
            return {
                "post_type": "message",
                "message_type": "group",
                "self_id": self_id,
                "group_id": peer_id,
                "user_id": sender_id,
                "message_id": encode_message_id(scene, peer_id, seq),
                "message": message,
                "sender": _sender_from_group_member(data),
            }
        return {
            "post_type": "message",
            "message_type": "private",
            "self_id": self_id,
            "user_id": sender_id,
            "message_id": encode_message_id(scene, peer_id, seq),
            "message": message,
            "sender": _sender_from_friend(data),
        }

    if event_type == "group_nudge":
        return {
            "post_type": "notice",
            "notice_type": "poke",
            "sub_type": "poke",
            "self_id": self_id,
            "group_id": int(data.get("group_id") or 0),
            "user_id": int(data.get("sender_id") or 0),
            "target_id": int(data.get("receiver_id") or 0),
        }

    if event_type == "friend_nudge" and data.get("is_self_receive") is True:
        return {
            "post_type": "notice",
            "notice_type": "poke",
            "sub_type": "poke",
            "self_id": self_id,
            "user_id": int(data.get("user_id") or 0),
            "target_id": self_id,
        }

    return None


def _sender_from_group_member(data: dict[str, Any]) -> dict[str, Any]:
    member = data.get("group_member") if isinstance(data.get("group_member"), dict) else {}
    nickname = str(member.get("card") or member.get("nickname") or "")
    return {"nickname": nickname, "card": str(member.get("card") or "")}


def _sender_from_friend(data: dict[str, Any]) -> dict[str, Any]:
    friend = data.get("friend") if isinstance(data.get("friend"), dict) else {}
    return {"nickname": str(friend.get("nickname") or "")}


def segment_onebot_to_milky(seg: Any) -> dict[str, Any] | None:
    """OneBot 出站消息段 -> Milky 消息段。"""
    if isinstance(seg, str):
        return {"type": "text", "data": {"text": seg}}
    if not isinstance(seg, dict):
        return None
    kind = str(seg.get("type") or "").lower()
    data = _data(seg)
    if kind == "text":
        return {"type": "text", "data": {"text": str(data.get("text") or "")}}
    if kind == "at":
        qq = str(data.get("qq") or "")
        if qq == "all":
            return {"type": "mention_all", "data": {}}
        if qq.isdigit():
            return {"type": "mention", "data": {"user_id": int(qq)}}
        return None
    if kind == "face":
        return {"type": "face", "data": {"face_id": str(data.get("id") or "")}}
    if kind == "image":
        uri = to_file_uri(data.get("file"))
        if not uri:
            return None
        sub_type = "sticker" if int(data.get("sub_type") or 0) == 1 else "normal"
        out = {"type": "image", "data": {"uri": uri, "sub_type": sub_type}}
        summary = str(data.get("summary") or "")
        if summary:
            out["data"]["summary"] = summary
        return out
    if kind == "record":
        uri = to_file_uri(data.get("file"))
        return {"type": "record", "data": {"uri": uri}} if uri else None
    if kind == "video":
        uri = to_file_uri(data.get("file"))
        return {"type": "video", "data": {"uri": uri}} if uri else None
    return None


def message_onebot_to_milky(message: Any) -> list[dict[str, Any]]:
    """OneBot 出站消息 -> Milky 消息段列表。"""
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    if not isinstance(message, list):
        return []
    result: list[dict[str, Any]] = []
    for seg in message:
        translated = segment_onebot_to_milky(seg)
        if translated is not None:
            result.append(translated)
    return result
