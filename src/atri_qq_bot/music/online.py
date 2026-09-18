from __future__ import annotations

"""在线点歌：本地歌库里没有的歌，通过网易云音乐公开 Web 接口搜索并下载，
再走和本地完整歌曲完全相同的 QQ 语音（record）发送链路。

搜索接口与音频直链都来自网易云的公开 Web 端点，仅供个人机器人自用；
VIP/付费歌曲拿不到完整音频时自动放弃，不会反复请求。
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..runtime.paths import PROJECT_ROOT
from .library import AUDIO_EXTENSIONS, DeliverySong, DeliverySongSegment
from .session import SingingDecision, _requested_title_fragment


LOGGER = logging.getLogger(__name__)

ONLINE_MUSIC_CACHE_DIR = PROJECT_ROOT / "data" / "music" / "online-cache"

_SEARCH_URL = "https://music.163.com/api/search/get/web"
_AUDIO_URL_TEMPLATE = "https://music.163.com/song/media/outer/url?id={song_id}.mp3"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}

_MIN_AUDIO_BYTES = 300_000
_MAX_AUDIO_BYTES = 25_000_000
_MIN_DURATION_MS = 60_000
_MAX_DURATION_MS = 10 * 60_000
_MAX_CACHE_FILES = 60
# 这些词出现在“歌名”位置时不是真实点歌，跳过在线搜索。
_BLOCKED_KEYWORDS = ("什么", "不知道", "随便", "哪首", "几首", "你自己", "你挑")


@dataclass(frozen=True)
class OnlineSong:
    song_id: int
    title: str
    artist: str
    duration_ms: int

    @property
    def display_title(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title


def online_search_keyword(user_text: str, model_song_title: str = "") -> str:
    """从用户原话里提取用于在线搜索的歌名。"""

    candidates = (
        _requested_title_fragment(user_text),
        str(model_song_title or "").strip(),
        str(user_text or "").strip(),
    )
    for candidate in candidates:
        value = re.sub(r"^[「『“\"']+|[」』”\"']+$", "", str(candidate or "").strip())
        value = re.sub(r"^(?:一首歌|一首|首歌|个|一段|那首|这首)", "", value)
        value = value.strip(" \t，,。.!！?？~～呗吧啊呀")
        if not value or len(value) < 1:
            continue
        if any(word in value for word in _BLOCKED_KEYWORDS):
            return ""
        return value[:60]
    return ""


async def search_online_songs(keyword: str, *, limit: int = 6) -> list[OnlineSong]:
    async with httpx.AsyncClient(timeout=8.0, headers=_HEADERS) as client:
        response = await client.get(
            _SEARCH_URL,
            params={"s": keyword, "type": 1, "limit": limit, "offset": 0},
        )
        response.raise_for_status()
        payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    items = result.get("songs") if isinstance(result, dict) else None
    songs: list[OnlineSong] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        artist = "、".join(
            str(artist.get("name") or "").strip()
            for artist in (item.get("artists") or [])
            if isinstance(artist, dict) and artist.get("name")
        )
        try:
            duration_ms = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        songs.append(
            OnlineSong(
                int(item["id"]),
                str(item.get("name") or "").strip(),
                artist,
                duration_ms,
            )
        )
    return songs


async def download_online_song(song: OnlineSong) -> Path | None:
    """下载完整音频到缓存目录；拿不到完整文件时返回 None。"""

    ONLINE_MUSIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = ONLINE_MUSIC_CACHE_DIR / f"netease-{song.song_id}.mp3"
    if target.is_file():
        size = target.stat().st_size
        if _MIN_AUDIO_BYTES <= size <= _MAX_AUDIO_BYTES:
            return target

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), headers=_HEADERS, follow_redirects=True
    ) as client:
        response = await client.get(_AUDIO_URL_TEMPLATE.format(song_id=song.song_id))
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code != 200 or not (
        content_type.startswith("audio/") or "octet-stream" in content_type
    ):
        LOGGER.info(
            "在线歌曲不可下载（VIP 或已下架）：song_id=%s title=%s status=%s type=%s",
            song.song_id,
            song.display_title,
            response.status_code,
            content_type,
        )
        return None
    data = response.content
    if not (_MIN_AUDIO_BYTES <= len(data) <= _MAX_AUDIO_BYTES):
        LOGGER.info(
            "在线歌曲大小异常，跳过：song_id=%s size=%s", song.song_id, len(data)
        )
        return None
    temporary = target.with_suffix(".part")
    temporary.write_bytes(data)
    temporary.replace(target)
    _prune_cache()
    return target


def online_song_decision(song: OnlineSong, path: Path) -> SingingDecision:
    segment = DeliverySongSegment(1, path, label="在线歌曲")
    delivery_song = DeliverySong(song.display_title, path.parent, (segment,))
    return SingingDecision(True, "online_play", delivery_song, segment)


def _normalise_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "", str(value or "").casefold())


def _keyword_matches_song(keyword: str, song: OnlineSong) -> bool:
    """搜索结果必须真的像用户点的歌，防止乱码/闲聊搜出随机歌曲。"""

    normalised_keyword = _normalise_text(keyword)
    if not normalised_keyword:
        return False
    haystack = _normalise_text(f"{song.title}{song.artist}")
    if not haystack:
        return False
    if normalised_keyword in haystack:
        return True
    distinct = set(normalised_keyword)
    overlap = len(distinct & set(haystack)) / len(distinct)
    return overlap >= 0.6


async def resolve_online_singing(
    user_text: str,
    *,
    model_song_title: str = "",
) -> SingingDecision | None:
    """本地歌库没有的歌，尝试在线搜索并下载，返回可发送决策。"""

    keyword = online_search_keyword(user_text, model_song_title)
    if not keyword:
        return None
    try:
        songs = await search_online_songs(keyword)
    except Exception as exc:
        LOGGER.warning("在线歌曲搜索失败：keyword=%s error=%s", keyword, exc)
        return None
    if not songs:
        return None
    preferred = [
        song
        for song in songs
        if _keyword_matches_song(keyword, song)
        and _MIN_DURATION_MS <= song.duration_ms <= _MAX_DURATION_MS
    ] or [
        song for song in songs if _keyword_matches_song(keyword, song)
    ]
    for song in preferred[:3]:
        try:
            path = await download_online_song(song)
        except Exception as exc:
            LOGGER.warning("在线歌曲下载失败：song_id=%s error=%s", song.song_id, exc)
            path = None
        if path is not None:
            LOGGER.info(
                "在线点歌命中：%s -> %s", keyword, path.name
            )
            return online_song_decision(song, path)
    return None


def _prune_cache() -> None:
    try:
        files = [
            item
            for item in ONLINE_MUSIC_CACHE_DIR.iterdir()
            if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS
        ]
        if len(files) <= _MAX_CACHE_FILES:
            return
        for item in sorted(files, key=lambda file: file.stat().st_mtime)[
            : len(files) - _MAX_CACHE_FILES
        ]:
            item.unlink(missing_ok=True)
    except OSError:
        pass
