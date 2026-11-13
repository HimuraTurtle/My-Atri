"""OneBot v11 动作 <-> Milky API 的翻译。

ATRI 只调用 8 个 OneBot 动作。翻译分两步：
- ``translate_action``：把 OneBot 动作转成 (milky_api, milky_body, context)，
  context 保存响应塑造所需的信息（如 get_msg 的 scene/peer_id）；
- ``shape_response``：把 Milky 响应 + context 整理成 OneBot 回包。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .protocol import decode_message_id, message_milky_to_onebot, message_onebot_to_milky, to_file_uri




def _file_name(body: dict[str, Any]) -> str:
    """取文件名：优先 name/file_name 参数，回退到 file 路径 basename。"""
    for key in ("name", "file_name"):
        value = str(body.get(key) or "").strip()
        if value:
            return value
    return Path(str(body.get("file") or "")).name

def translate_action(action: str, params: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """OneBot 动作 -> (milky_api, milky_body, context)；不支持返回 None。"""
    name = str(action or "").strip()
    body = params if isinstance(params, dict) else {}

    if name == "get_status":
        return "get_login_info", {}, {}
    if name == "get_cookies":
        return "get_cookies", {"domain": str(body.get("domain") or "")}, {}
    if name == "get_group_list":
        return "get_group_list", {}, {}
    if name == "get_msg":
        decoded = decode_message_id(str(body.get("message_id") or ""))
        if decoded is None:
            return None
        scene, peer_id, seq = decoded
        return (
            "get_message",
            {"message_scene": scene, "peer_id": peer_id, "message_seq": seq},
            {"scene": scene, "peer_id": peer_id},
        )
    if name == "send_private_msg":
        return "send_private_message", {
            "user_id": int(body.get("user_id") or 0),
            "message": message_onebot_to_milky(body.get("message")),
        }, {}
    if name == "send_group_msg":
        return "send_group_message", {
            "group_id": int(body.get("group_id") or 0),
            "message": message_onebot_to_milky(body.get("message")),
        }, {}
    if name == "upload_group_file":
        return "upload_group_file", {
            "group_id": int(body.get("group_id") or 0),
            "file_uri": to_file_uri(body.get("file")),
            "file_name": _file_name(body),
        }, {}
    if name == "upload_private_file":
        return "upload_private_file", {
            "user_id": int(body.get("user_id") or 0),
            "file_uri": to_file_uri(body.get("file")),
            "file_name": _file_name(body),
        }, {}
    return None


def shape_response(
    action: str,
    echo: str,
    milky_resp: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """把 Milky 响应 + context 整理成 OneBot 回包（含 echo）。"""
    name = str(action or "").strip()
    status = str(milky_resp.get("status") or "ok").lower()
    retcode = int(milky_resp.get("retcode") or 0)
    data = milky_resp.get("data") if isinstance(milky_resp.get("data"), dict) else {}
    message = str(milky_resp.get("message") or "")

    if status == "ok" and retcode == 0:
        if name == "get_status":
            data = {
                "online": True,
                "good": True,
                "uin": int(data.get("uin") or 0),
                "nickname": str(data.get("nickname") or ""),
            }
        elif name == "get_group_list":
            groups = data.get("groups") if isinstance(data.get("groups"), list) else []
            data = [
                {"group_id": str(g.get("group_id") or ""), "group_name": str(g.get("group_name") or "")}
                for g in groups
                if isinstance(g, dict)
            ]
        elif name == "get_msg":
            scene = str(context.get("scene") or "")
            peer_id = int(context.get("peer_id") or 0)
            raw = data.get("message") if isinstance(data.get("message"), dict) else None
            data = {"message": message_milky_to_onebot(raw, scene, peer_id)} if raw is not None else {}

    return {
        "echo": echo,
        "status": status,
        "retcode": retcode,
        "data": data,
        "message": message,
    }
