"""对称修图的图片获取：从消息段定位图片并下载字节流。

候选链与工具箱 `_resolve_ref_material` 同构：段内 url → 本地缓存路径
（NapCat 与机器人同机）→ get_image 动作回退。任何一步失败都返回
None，由命令层坦诚告知，绝不编造结果。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.image_edit")

_DOWNLOAD_TIMEOUT_SECONDS = 20.0
_MAX_IMAGE_BYTES = 30 * 1024 * 1024

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def first_image_segment(segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取第一个普通图片段（闪照/mface 商店表情不参与修图）。"""
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        data = segment.get("data") or {}
        if data.get("flash"):
            continue
        return segment
    return None


async def download_image(url: str) -> bytes:
    """流式下载图片字节，超过大小上限时中止并抛错。"""
    received = 0
    chunks: list[bytes] = []
    async with httpx.AsyncClient(
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=_HEADERS,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > _MAX_IMAGE_BYTES:
                    raise ValueError("图片超过 30MB，无法处理")
                chunks.append(chunk)
    return b"".join(chunks)


def _looks_like_local_path(value: str) -> bool:
    return bool(value) and len(value) > 3 and ("/" in value or "\\" in value)


async def load_segment_image_bytes(
    segment: dict[str, Any],
    action_caller: Any | None = None,
) -> bytes | None:
    """按候选链读取图片段字节：url → 本地路径 → get_image 动作。"""
    data = segment.get("data") or {}

    url = str(data.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        try:
            return await download_image(url)
        except Exception as exc:
            LOGGER.warning("图片下载失败 url=%s error=%s", url[:120], exc)

    file_ref = str(data.get("file") or "").strip()
    if _looks_like_local_path(file_ref) and Path(file_ref).is_file():
        try:
            return Path(file_ref).read_bytes()
        except OSError as exc:
            LOGGER.warning("本地图片读取失败 path=%s error=%s", file_ref, exc)

    if action_caller is None or not file_ref:
        return None
    try:
        result = await action_caller("get_image", {"file": file_ref}, 6.0)
    except Exception as exc:
        LOGGER.warning("get_image 回退失败 file=%s error=%s", file_ref[:80], exc)
        return None
    payload = result.get("data") if isinstance(result, dict) else None
    payload = payload if isinstance(payload, dict) else {}

    fallback_url = str(payload.get("url") or "").strip()
    if fallback_url.startswith(("http://", "https://")):
        try:
            return await download_image(fallback_url)
        except Exception as exc:
            LOGGER.warning("回退图片下载失败 error=%s", exc)

    local = str(payload.get("file") or "").strip()
    if _looks_like_local_path(local) and Path(local).is_file():
        try:
            return Path(local).read_bytes()
        except OSError as exc:
            LOGGER.warning("回退本地图片读取失败 path=%s error=%s", local, exc)
    return None
