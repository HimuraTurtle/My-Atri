"""QQ 空间 Web API 客户端：httpx 单例 + JSON/JSONP 自研解析。

端点契约取自 astrbot_plugin_qzone 的 QzoneAPI；解析层不引入 json5/bs4：
- 响应统一走 parse_response（callback 包裹、undefined、尾逗号容错）；
- 好友动态流的 html 片段用正则抽取纯文本，替代 BeautifulSoup。
所有方法返回 ``{"ok": bool, "message": str, "data": dict}``，错误信息用中文。
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any

import httpx

from .session import QzoneContext, QzoneSession

_BASE = "https://user.qzone.qq.com"
_EMOTION_URL = f"{_BASE}/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
_DOLIKE_URL = f"{_BASE}/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
_LIST_URL = f"{_BASE}/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
_COMMENT_URL = f"{_BASE}/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
_ZONE_LIST_URL = f"{_BASE}/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"

_CALLBACK_RE = re.compile(r"callback\s*\(\s*([^{]*?(\{.*\})[^)]*?)\s*\)", re.I | re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_TEXT_TAG_RE = re.compile(r"<[^>]+>")

_HTTP_STATUS_KEY = "_http_status"


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message, "data": {}}


def parse_response(text: str) -> dict[str, Any]:
    """JSON / JSONP / 容错 JSON → dict；失败返回带中文错误的失败结构。"""
    raw = str(text or "").strip()
    if not raw:
        return _error("QQ 空间接口返回空响应")
    match = _CALLBACK_RE.search(raw)
    payload = match.group(2) if match else raw
    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1 or end < start:
        return _error("QQ 空间接口响应缺少 JSON 片段")
    payload = payload[start : end + 1].replace("undefined", "null").strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        try:
            data = json.loads(_TRAILING_COMMA_RE.sub(r"\1", payload))
        except json.JSONDecodeError:
            return _error("QQ 空间接口响应不是合法 JSON")
    if not isinstance(data, dict):
        return _error("QQ 空间接口响应不是对象结构")
    return data


def _html_text(fragment: str) -> str:
    """从动态流 html 片段抽取纯文本（替代 bs4 的 f-info 提取）。"""
    text = str(fragment or "")
    match = re.search(r'<div[^>]*class="f-info"[^>]*>(.*?)</div>', text, re.S)
    if match:
        text = match.group(1)
    else:
        text = re.sub(r"<script.*?</script>", "", text, flags=re.S)
    plain = html.unescape(_TEXT_TAG_RE.sub(" ", text))
    return " ".join(plain.split()).strip()


class QzoneAPI:
    """无状态 HTTP 封装；每次请求现取会话 Cookie，失败时由调用方重试。"""

    def __init__(self, session: QzoneSession, *, timeout_seconds: float = 20.0) -> None:
        self.session = session
        self._timeout = max(5.0, float(timeout_seconds))
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
        return self._client

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx = await self.session.get_ctx()
        client = self._ensure_client()
        response = await client.request(
            method,
            url,
            params=params,
            data=data,
            cookies=ctx.cookies(),
            headers=ctx.headers(),
        )
        parsed = parse_response(await response.text())
        parsed[_HTTP_STATUS_KEY] = response.status_code
        return parsed

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """带一次登录失效自愈的请求：401/code=1001 时刷新 Cookie 重试。"""
        parsed = await self._request(method, url, params=params, data=data)
        status = int(parsed.get(_HTTP_STATUS_KEY) or 0)
        code = parsed.get("code")
        if status in (401, 403) or code in (-1000, 1001):
            await self.session.invalidate()
            parsed = await self._request(method, url, params=params, data=data)
        return parsed

    # ---- 业务端点 ----

    async def publish(self, text: str, *, visibility: int = 1) -> dict[str, Any]:
        """发表说说；成功返回 tid。"""
        ctx: QzoneContext = await self.session.get_ctx()
        data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": str(max(0, min(2, int(visibility)))),
            "to_sign": "0",
            "hostuin": ctx.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"{_BASE}/{ctx.uin}",
        }
        parsed = await self.request(
            "POST", _EMOTION_URL, params={"g_tk": ctx.gtk2, "uin": ctx.uin}, data=data
        )
        if str(parsed.get("code")) not in {"0"}:
            return _error(
                f"发布说说失败：{parsed.get('message') or parsed.get('code') or '接口未确认'}"
            )
        # 发布接口返回扁平结构（code/tid/now 同级），部分版本会再包一层 data。
        payload = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        return {
            "ok": True,
            "message": "",
            "data": {
                "tid": str(payload.get("tid") or ""),
                "abstime": int(payload.get("now") or payload.get("abstime") or 0),
            },
        }

    async def like(self, target_uin: int, tid: str) -> dict[str, Any]:
        ctx = await self.session.get_ctx()
        key = f"{_BASE}/{target_uin}/mood/{tid}"
        parsed = await self.request(
            "POST",
            _DOLIKE_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "qzreferrer": f"{_BASE}/{ctx.uin}",
                "opuin": ctx.uin,
                "unikey": key,
                "curkey": key,
                "appid": 311,
                "from": 1,
                "typeid": 0,
                "abstime": int(time.time()),
                "fid": tid,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            },
        )
        if str(parsed.get("code")) not in {"0"}:
            return _error(f"点赞失败：{parsed.get('message') or parsed.get('code') or '接口未确认'}")
        return {"ok": True, "message": "", "data": {"tid": tid, "uin": int(target_uin)}}

    async def comment(self, target_uin: int, tid: str, content: str) -> dict[str, Any]:
        ctx = await self.session.get_ctx()
        parsed = await self.request(
            "POST",
            _COMMENT_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "topicId": f"{target_uin}_{tid}__1",
                "uin": ctx.uin,
                "hostUin": int(target_uin),
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
            },
        )
        if str(parsed.get("code")) not in {"0"}:
            return _error(f"评论失败：{parsed.get('message') or parsed.get('code') or '接口未确认'}")
        return {
            "ok": True,
            "message": "",
            "data": {"tid": tid, "uin": int(target_uin), "content": content},
        }

    async def get_feeds(
        self, target_uin: int | str, *, pos: int = 0, num: int = 10
    ) -> dict[str, Any]:
        """读取指定 QQ 的说说列表（msglist 接口，结构化返回）。"""
        ctx = await self.session.get_ctx()
        parsed = await self.request(
            "GET",
            _LIST_URL,
            params={
                "g_tk": ctx.gtk2,
                "uin": int(target_uin),
                "ftype": 0,
                "sort": 0,
                "pos": max(0, int(pos)),
                "num": max(1, min(30, int(num))),
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
        )
        if str(parsed.get("code")) not in {"0"}:
            return _error(
                f"读取说说失败：{parsed.get('message') or parsed.get('code') or '接口未确认'}"
            )
        msglist = parsed.get("msglist") or []
        if not isinstance(msglist, list):
            msglist = []
        return {"ok": True, "message": "", "data": {"feeds": [self._to_feed(item) for item in msglist]}}

    async def get_recent_feeds(self, *, num: int = 20) -> dict[str, Any]:
        """读取好友动态流（feeds3 接口，html 解析）。"""
        ctx = await self.session.get_ctx()
        parsed = await self.request(
            "GET",
            _ZONE_LIST_URL,
            params={
                "uin": ctx.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": 1,
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": 0,
                "format": "json",
                "g_tk": ctx.gtk2,
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
        )
        if str(parsed.get("code")) not in {"0"}:
            return _error(
                f"读取好友动态失败：{parsed.get('message') or parsed.get('code') or '接口未确认'}"
            )
        container = parsed.get("data") or {}
        entries = container.get("data") or []
        if not isinstance(entries, list):
            entries = []
        feeds: list[dict[str, Any]] = []
        for entry in entries:
            feed = self._feed_from_html(entry)
            if feed is not None:
                feeds.append(feed)
        return {"ok": True, "message": "", "data": {"feeds": feeds[: max(1, int(num))]}}

    # ---- 归一化 ----

    @staticmethod
    def _to_feed(msg: dict[str, Any]) -> dict[str, Any]:
        comments = []
        for item in msg.get("commentlist") or []:
            if not isinstance(item, dict):
                continue
            comments.append(
                {
                    "tid": str(item.get("tid") or ""),
                    "uin": int(item.get("uin") or 0),
                    "name": str(item.get("name") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        return {
            "tid": str(msg.get("tid") or ""),
            "uin": int(msg.get("uin") or 0),
            "name": str(msg.get("name") or ""),
            "content": str(msg.get("content") or "").strip(),
            "create_time": int(msg.get("created_time") or 0),
            "comments": comments,
            "like_count": 0,
        }

    @staticmethod
    def _feed_from_html(entry: dict[str, Any]) -> dict[str, Any] | None:
        if str(entry.get("appid") or "") != "311":
            return None
        uin = str(entry.get("uin") or "").strip()
        tid = str(entry.get("key") or "").strip()
        if not uin.isdigit() or not tid:
            return None
        content = _html_text(str(entry.get("html") or ""))
        if not content:
            return None
        return {
            "tid": tid,
            "uin": int(uin),
            "name": str(entry.get("nickname") or ""),
            "content": content,
            "create_time": int(entry.get("abstime") or 0),
            "comments": [],
            "like_count": 0,
        }
