"""B站 HTTP 客户端：httpx 实现 + WBI 签名 + 视频/字幕/播放地址 API。

移植自 astrbot_plugin_bilivideo 的 api/client.py + wbi.py，网络层从
aiohttp 换成 httpx（ATRI 依赖约定）。短链解析刻意不带登录 Cookie，
避免把凭据泄露给重定向目标。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.bilibili")

API_BASE = "https://api.bilibili.com"
ENDPOINT_NAV = f"{API_BASE}/x/web-interface/nav"
ENDPOINT_VIEW = f"{API_BASE}/x/web-interface/view"
ENDPOINT_VIEW_BY_AID = f"{API_BASE}/x/web-interface/view"
ENDPOINT_PLAYER = f"{API_BASE}/x/player/wbi/v2"
ENDPOINT_PLAYURL = f"{API_BASE}/x/player/playurl"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_REFERER = "https://www.bilibili.com"
HTTP_TIMEOUT_SECONDS = 15.0
HTTP_MAX_RETRIES = 3
HTTP_BACKOFF_BASE = 0.6
WBI_CACHE_TTL_SECONDS = 86_400

_WBI_MIXIN_TABLE: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


class BilibiliError(Exception):
    """面向用户的可读错误（消息会直接回复给群友）。"""


class BilibiliNetworkError(BilibiliError):
    pass


@dataclass(slots=True)
class VideoInfo:
    bvid: str
    aid: int
    title: str
    pic: str
    desc: str
    pubdate: int
    duration: int
    cid: int
    pages: int
    owner_name: str
    owner_mid: int
    view: int
    danmaku: int
    like: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"


@dataclass(slots=True)
class SubtitleTrack:
    lan: str
    lan_doc: str
    subtitle_url: str
    ai_type: int


def _normalize_pic(pic: str) -> str:
    return ("https:" + pic) if pic.startswith("//") else pic


class BilibiliApiClient:
    """共享一个 httpx.AsyncClient 的轻量 B站 API 客户端。"""

    def __init__(self, cookie: str = "") -> None:
        self._cookie: dict[str, str] = {}
        for part in (cookie or "").split(";"):
            if "=" in part:
                name, _, value = part.partition("=")
                if name.strip() and value.strip():
                    self._cookie[name.strip()] = value.strip()
        # buvid3 是搜索/播放接口的风控必要 cookie，匿名时随机生成即可。
        if "buvid3" not in self._cookie:
            self._cookie["buvid3"] = f"{uuid.uuid4()}infoc"
        self._client: httpx.AsyncClient | None = None
        self._wbi_key: str | None = None
        self._wbi_key_at: float = 0.0
        self._wbi_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def _headers(self, *, with_cookie: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": DEFAULT_REFERER,
        }
        if with_cookie and self._cookie:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookie.items() if v)
        return headers

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_code_zero: bool = True,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        merged = self._headers()
        if headers:
            merged.update(headers)
        last_error: Exception | None = None
        for attempt in range(1, HTTP_MAX_RETRIES + 1):
            try:
                response = await client.request(method, url, params=params, headers=merged)
                if response.status_code >= 400:
                    raise BilibiliNetworkError(f"HTTP {response.status_code} from {url}")
                payload = response.json()
                if expect_code_zero and isinstance(payload.get("code"), int) and payload["code"] != 0:
                    code = payload["code"]
                    message = str(payload.get("message", ""))
                    if code in (-412, 412, -352):
                        raise BilibiliNetworkError("触发B站风控，请稍后再试或配置 BILIBILI_COOKIE")
                    raise BilibiliError(f"B站接口错误 code={code}: {message}")
                return payload
            except (httpx.HTTPError, BilibiliNetworkError) as exc:
                last_error = exc
                if attempt == HTTP_MAX_RETRIES:
                    break
                delay = HTTP_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                await asyncio.sleep(delay)
        raise BilibiliNetworkError(f"请求失败: {last_error}")

    async def follow_redirect(self, url: str) -> str | None:
        """解析 b23.tv 短链；不带登录 Cookie，防止凭据泄露。"""
        client = await self._ensure_client()
        try:
            response = await client.get(
                url, headers=self._headers(with_cookie=False), follow_redirects=True
            )
            return str(response.url)
        except httpx.HTTPError as exc:
            LOGGER.warning("短链解析失败 %s: %s", url, exc)
            return None

    # ─────────────────────────── WBI 签名 ───────────────────────────

    async def _get_wbi_key(self) -> str | None:
        async with self._wbi_lock:
            if self._wbi_key and time.time() - self._wbi_key_at < WBI_CACHE_TTL_SECONDS:
                return self._wbi_key
            try:
                payload = await self.request_json("GET", ENDPOINT_NAV, expect_code_zero=False)
            except BilibiliError as exc:
                LOGGER.warning("WBI key 获取失败，使用未签名请求兜底: %s", exc)
                return None
            wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
            img_url = str(wbi_img.get("img_url", ""))
            sub_url = str(wbi_img.get("sub_url", ""))
            if not img_url or not sub_url:
                return None
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
            orig = img_key + sub_key
            self._wbi_key = "".join(orig[i] for i in _WBI_MIXIN_TABLE if i < len(orig))[:32]
            self._wbi_key_at = time.time()
            return self._wbi_key

    async def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        mixin_key = await self._get_wbi_key()
        signed = dict(params)
        if not mixin_key:
            return signed
        signed["wts"] = int(time.time())
        query = urllib.parse.urlencode(sorted(signed.items()))
        for ch in "!'()*":
            query = query.replace(urllib.parse.quote(ch), "")
        signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return signed

    # ─────────────────────────── 业务 API ───────────────────────────

    async def get_video_info(self, bvid_or_aid: str) -> VideoInfo:
        params: dict[str, Any]
        if bvid_or_aid.lower().startswith("bv"):
            params = {"bvid": bvid_or_aid}
        else:
            params = {"aid": bvid_or_aid.lstrip("aAvV")}
        payload = await self.request_json("GET", ENDPOINT_VIEW, params=params)
        data = payload.get("data") or {}
        if not data:
            raise BilibiliError("视频不存在或已被删除")
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        pages = data.get("pages") or []
        cid = int(data.get("cid") or (pages[0].get("cid") if pages else 0) or 0)
        return VideoInfo(
            bvid=str(data.get("bvid") or bvid_or_aid),
            aid=int(data.get("aid") or 0),
            title=str(data.get("title", "")),
            pic=_normalize_pic(str(data.get("pic", ""))),
            desc=str(data.get("desc", "")),
            pubdate=int(data.get("pubdate", 0) or 0),
            duration=int(data.get("duration", 0) or 0),
            cid=cid,
            pages=int(data.get("videos") or len(pages) or 1),
            owner_name=str(owner.get("name", "未知")),
            owner_mid=int(owner.get("mid", 0) or 0),
            view=int(stat.get("view", 0) or 0),
            danmaku=int(stat.get("danmaku", 0) or 0),
            like=int(stat.get("like", 0) or 0),
            raw=data,
        )

    async def get_page_cid(self, bvid: str, page: int) -> int:
        if page <= 1:
            return 0
        payload = await self.request_json("GET", ENDPOINT_VIEW, params={"bvid": bvid})
        pages = (payload.get("data") or {}).get("pages") or []
        if 1 <= page <= len(pages):
            return int(pages[page - 1].get("cid") or 0)
        return 0

    async def get_subtitle_tracks(self, bvid: str, cid: int) -> list[SubtitleTrack]:
        params = await self._sign_params({"bvid": bvid, "cid": cid})
        payload = await self.request_json("GET", ENDPOINT_PLAYER, params=params)
        subtitles = (((payload.get("data") or {}).get("subtitle")) or {}).get("subtitles") or []
        tracks: list[SubtitleTrack] = []
        for item in subtitles:
            url = str(item.get("subtitle_url", ""))
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                continue
            tracks.append(
                SubtitleTrack(
                    lan=str(item.get("lan", "")),
                    lan_doc=str(item.get("lan_doc", "")),
                    subtitle_url=url,
                    ai_type=int(item.get("ai_type") or item.get("ai_status") or 0),
                )
            )
        return tracks

    async def get_audio_url(self, bvid: str, cid: int) -> str | None:
        """获取 DASH 音频流地址（ASR 转写用，取最低码率节省流量）。"""
        params = {"bvid": bvid, "cid": cid, "fnval": 16, "qn": 16}
        payload = await self.request_json("GET", ENDPOINT_PLAYURL, params=params)
        dash = (payload.get("data") or {}).get("dash") or {}
        audio_streams = dash.get("audio") or []
        if not audio_streams:
            return None
        best = min(
            audio_streams,
            key=lambda item: int(item.get("bandwidth") or 0),
        )
        url = str(best.get("baseUrl") or best.get("base_url") or "")
        return url or None
