"""字幕获取与语音转写。

优先级：平台字幕（CC/AI 字幕）→ DASH 音频下载 + 必剪(BCut) ASR。
移植自 astrbot_plugin_bilivideo 的 transcription/ 子包，去掉了 yt-dlp
依赖：音频直接走 B站 playurl DASH 流，上传 ASR 前无需 ffmpeg 转码。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .client import BilibiliApiClient, BilibiliError

LOGGER = logging.getLogger("atri.bilibili")

SUBTITLE_LANG_PREFERENCE: tuple[str, ...] = ("zh-Hans", "zh-CN", "zh", "ai-zh", "en-US", "en")
ASR_POLL_INTERVAL_SECONDS = 1.0
ASR_POLL_MAX_TRIES = 180
AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 180.0

_BCUT_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
_BCUT_HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}


@dataclass(slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(slots=True)
class Transcript:
    segments: list[TranscriptSegment]
    source: str  # "subtitle" | "asr"
    language: str = ""


async def fetch_subtitle_transcript(
    client: BilibiliApiClient, bvid: str, cid: int
) -> Transcript | None:
    """拉取平台字幕并解析为分段；无字幕返回 None。"""
    tracks = await client.get_subtitle_tracks(bvid, cid)
    if not tracks:
        return None
    track = _pick_track(tracks)
    if track is None:
        return None
    payload = await client.request_json("GET", track.subtitle_url, expect_code_zero=False)
    body = payload.get("body") or []
    segments: list[TranscriptSegment] = []
    for item in body:
        text = str(item.get("content", "")).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start=float(item.get("from", 0) or 0),
                end=float(item.get("to", 0) or 0),
                text=text,
            )
        )
    if not segments:
        return None
    return Transcript(segments=segments, source="subtitle", language=track.lan)


def _pick_track(tracks: list[Any]) -> Any | None:
    for lang in SUBTITLE_LANG_PREFERENCE:
        for track in tracks:
            if track.lan == lang:
                return track
    return tracks[0] if tracks else None


async def download_audio(
    client: BilibiliApiClient, bvid: str, cid: int, dest_dir: Path
) -> Path | None:
    """下载 DASH 音频流（m4s，本质是 AAC 裸流，ASR 可直接读）。"""
    audio_url = await client.get_audio_url(bvid, cid)
    if not audio_url:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{bvid}.m4s"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    http = await client._ensure_client()
    headers = client._headers()
    headers["Range"] = "bytes=0-"
    try:
        async with asyncio.timeout(AUDIO_DOWNLOAD_TIMEOUT_SECONDS):
            async with http.stream("GET", audio_url, headers=headers) as response:
                if response.status_code >= 400:
                    LOGGER.warning("音频下载失败 HTTP %s: %s", response.status_code, bvid)
                    return None
                with dest.open("wb") as fp:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        fp.write(chunk)
    except (httpx.HTTPError, TimeoutError) as exc:
        LOGGER.warning("音频下载异常 %s: %s", bvid, exc)
        return None
    return dest if dest.exists() and dest.stat().st_size > 0 else None


async def bcut_asr(file_path: Path) -> Transcript | None:
    """必剪 ASR：分片上传 → 建任务 → 轮询结果。失败返回 None。"""
    try:
        return await _bcut_transcribe(file_path)
    except Exception as exc:
        LOGGER.warning("BCut ASR 失败: %s", exc)
        return None


async def _bcut_transcribe(file_path: Path) -> Transcript:
    data = file_path.read_bytes()
    if not data:
        raise BilibiliError("音频文件为空")
    async with httpx.AsyncClient(timeout=30.0) as http:
        meta = await _bcut_post(http, f"{_BCUT_BASE}/resource/create", {
            "type": 2,
            "name": "audio.mp3",
            "size": len(data),
            "ResourceFileType": "mp3",
            "model_id": "8",
        })
        upload_urls = meta["upload_urls"]
        per_size = int(meta["per_size"])
        etags: list[str] = []
        for clip, url in enumerate(upload_urls):
            start = clip * per_size
            end = min((clip + 1) * per_size, len(data))
            response = await http.put(
                url,
                content=data[start:end],
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            etags.append(response.headers.get("Etag", "").strip('"'))
        commit = await _bcut_post(http, f"{_BCUT_BASE}/resource/create/complete", {
            "InBossKey": meta["in_boss_key"],
            "ResourceId": meta["resource_id"],
            "Etags": ",".join(etags),
            "UploadId": meta["upload_id"],
            "model_id": "8",
        })
        task = await _bcut_post(http, f"{_BCUT_BASE}/task", {
            "resource": commit["download_url"],
            "model_id": "8",
        })
        task_id = task["task_id"]

        for attempt in range(ASR_POLL_MAX_TRIES):
            response = await http.get(
                f"{_BCUT_BASE}/task/result",
                params={"model_id": 7, "task_id": task_id},
                headers=_BCUT_HEADERS,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise BilibiliError(f"BCut 查询失败: {payload.get('message', '')}")
            result = payload.get("data") or {}
            state = result.get("state")
            if state == 4:
                return _parse_bcut_result(result)
            if state == 3:
                raise BilibiliError("BCut 转写任务失败")
            if attempt and attempt % 30 == 0:
                LOGGER.info("BCut ASR 轮询中 %s/%s", attempt, ASR_POLL_MAX_TRIES)
            await asyncio.sleep(ASR_POLL_INTERVAL_SECONDS)
    raise BilibiliError("BCut 转写超时")


def _parse_bcut_result(result: dict[str, Any]) -> Transcript:
    result_json = json.loads(result["result"])
    segments: list[TranscriptSegment] = []
    for utterance in result_json.get("utterances", []):
        text = str(utterance.get("transcript") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start=float(utterance.get("start_time", 0)) / 1000.0,
                end=float(utterance.get("end_time", 0)) / 1000.0,
                text=text,
            )
        )
    if not segments:
        raise BilibiliError("BCut 转写结果为空")
    return Transcript(
        segments=segments,
        source="asr",
        language=str(result_json.get("language", "zh")),
    )


async def acquire_transcript(
    client: BilibiliApiClient,
    bvid: str,
    cid: int,
    *,
    audio_dir: Path,
    asr_enabled: bool,
) -> tuple[Transcript | None, Path | None]:
    """完整获取链：字幕优先，无字幕时（可选）下载音频走 ASR。

    返回 (transcript, audio_path)；audio_path 由调用方负责清理。
    """
    transcript = await fetch_subtitle_transcript(client, bvid, cid)
    if transcript is not None:
        return transcript, None
    if not asr_enabled:
        return None, None
    audio_path = await download_audio(client, bvid, cid, audio_dir)
    if audio_path is None:
        return None, None
    transcript = await bcut_asr(audio_path)
    if transcript is None:
        return None, audio_path
    return transcript, audio_path
