"""B站视频标识提取：纯函数，不做网络 IO。

移植自 astrbot_plugin_bilivideo 的 parsing/url_extractor.py，裁剪掉
YouTube/抖音分支；QQ 分享卡片场景直接读取原始消息段中的 json 数据，
比从渲染后的纯文本里抓 URL 更可靠。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

BILI_DOMAINS: tuple[str, ...] = (
    "bilibili.com",
    "b23.tv",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bili33.cn",
)
SHORT_URL_DOMAINS: frozenset[str] = frozenset({"b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn", "bili33.cn"})

BV_REGEX = re.compile(r"BV[0-9A-Za-z]{10}")
AV_REGEX = re.compile(r"\bav(\d{6,})\b", re.IGNORECASE)
LONG_URL_REGEX = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/[A-Za-z0-9/?=&_.\-]+")
SHORT_URL_REGEX = re.compile(
    r"https?://(?:b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn|bili33\.cn)/[A-Za-z0-9]+",
    re.IGNORECASE,
)
GENERIC_URL_RE = re.compile(r"https?://[^\s<>\"，。）、）！!？?；;：:]+", re.IGNORECASE)

_TRAILING_URL_CHARS = "\"'`}>]),，。)、）！!？?；;：:"

# av 号会撞上英文单词（如 "save123"），仅当文本里没有 BV/链接时才回退使用。


def clean_url_token(url: str) -> str:
    return (url or "").strip().strip("<>").rstrip(_TRAILING_URL_CHARS)


def is_bilibili_domain(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(clean_url_token(url)).hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in BILI_DOMAINS)


def is_short_bili_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(clean_url_token(url)).hostname or ""
    except ValueError:
        return False
    return host.lower().rstrip(".") in SHORT_URL_DOMAINS


def extract_bvid(text: str) -> str | None:
    if not text:
        return None
    match = BV_REGEX.search(text)
    return match.group(0) if match else None


def extract_aid(text: str) -> str | None:
    if not text:
        return None
    match = AV_REGEX.search(text)
    return ("av" + match.group(1)) if match else None


def extract_long_url(text: str) -> str | None:
    if not text:
        return None
    match = LONG_URL_REGEX.search(text)
    return clean_url_token(match.group(0)) if match else None


def extract_short_url(text: str) -> str | None:
    if not text:
        return None
    match = SHORT_URL_REGEX.search(text)
    return clean_url_token(match.group(0)) if match else None


def extract_generic_url(text: str) -> str | None:
    if not text:
        return None
    match = GENERIC_URL_RE.search(text)
    return clean_url_token(match.group(0)) if match else None


def _find_bili_url_in_json(payload: Any) -> str | None:
    """递归搜索 JSON 卡片中的 B站链接（qqdocurl/jumpUrl/url 任一字段）。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("qqdocurl", "jumpUrl", "url") and isinstance(value, str) and is_bilibili_domain(value):
                return value
        for value in payload.values():
            found = _find_bili_url_in_json(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_bili_url_in_json(item)
            if found:
                return found
    return None


def url_from_json_card_text(text: str) -> str | None:
    """从字符串形式的 JSON 卡片（或含 qqdocurl 的任意文本）提取 B站链接。"""
    if not text or ("qqdocurl" not in text and "jumpUrl" not in text and not text.strip().startswith(("{", "["))):
        return None
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            return _find_bili_url_in_json(json.loads(stripped))
        except json.JSONDecodeError:
            pass
    return _find_bili_url_in_json({"qqdocurl": _first_quoted_url(text)})


def _first_quoted_url(text: str) -> str | None:
    match = re.search(r'"(?:qqdocurl|jumpUrl|url)"\s*:\s*"(https?://[^"]+)"', text)
    return match.group(1) if match else None


def url_from_event(event: dict[str, Any]) -> str | None:
    """从原始 OneBot 事件中提取 B站视频链接（json 卡片优先，其次文本段）。"""
    text_pool: list[str] = []
    for segment in event.get("message") or []:
        if not isinstance(segment, dict):
            continue
        seg_type = segment.get("type")
        data = segment.get("data") or {}
        if seg_type == "json":
            for value in (data.get("data"), data):
                if isinstance(value, str):
                    found = url_from_json_card_text(value)
                    if found:
                        return found
                elif isinstance(value, dict):
                    found = _find_bili_url_in_json(value)
                    if found:
                        return found
        elif seg_type == "text" and isinstance(data.get("text"), str):
            text_pool.append(data["text"])
    for text in text_pool:
        url = extract_long_url(text) or extract_short_url(text)
        if url:
            return url
        bvid = extract_bvid(text)
        if bvid:
            return f"https://www.bilibili.com/video/{bvid}"
    return None


def extract_page_param(url: str) -> int:
    """提取 ?p=N 分P参数，默认 1。"""
    try:
        query = urllib.parse.urlparse(url).query
        value = urllib.parse.parse_qs(query).get("p", ["1"])[0]
        return max(1, int(value))
    except (ValueError, IndexError):
        return 1


def message_is_pure_link(plain_text: str, event: dict[str, Any]) -> bool:
    """判断消息是否「本质是一条视频分享」：除链接/分享卡片外没有多余文字。

    自动总结只在这种消息上触发，避免群里随口提到一个链接就被打断。
    """
    text = (plain_text or "").strip()
    if not text:
        return any(
            isinstance(seg, dict) and seg.get("type") in {"json", "xml", "share"}
            for seg in (event.get("message") or [])
        )
    stripped = text
    for pattern in (
        LONG_URL_REGEX,
        SHORT_URL_REGEX,
        GENERIC_URL_RE,
    ):
        stripped = pattern.sub(" ", stripped)
    stripped = BV_REGEX.sub(" ", stripped)
    stripped = stripped.replace("[分享:", " ").replace("]", " ")
    # 剩余字符（去空白）超过阈值说明还有别的话要说
    return len("".join(stripped.split())) <= 12
