"""热梗抓取：从公开热搜榜接口拉取当前热词，供 LLM 精选成可用的梗库。

只依赖免登录的公开 JSON 端点（百度实时热搜、B 站热搜榜）。任一来源失败
只影响该来源，不影响其他来源；全部失败时返回空列表由上层跳过本轮。
"""

from __future__ import annotations

import asyncio
import logging

import httpx


LOGGER = logging.getLogger("atri.memes.fetcher")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    )
}

_TIMEOUT_SECONDS = 10.0

_MAX_WORDS_PER_SOURCE = 40


async def fetch_raw_trends() -> list[dict[str, str]]:
    """并发拉取全部来源，返回 ``[{"source", "word"}]``，按来源稳定去重。"""

    fetchers = (
        ("百度热搜", _fetch_baidu_hot),
        ("B站热搜", _fetch_bilibili_hot),
    )
    results = await asyncio.gather(*(fetcher() for _, fetcher in fetchers))
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for (source_name, _), result in zip(fetchers, results):
        if isinstance(result, BaseException):
            LOGGER.warning("热梗来源 %s 抓取失败：%s", source_name, result)
            continue
        words = [word for word in result if word and word not in seen]
        seen.update(words)
        merged.extend({"source": source_name, "word": word} for word in words)
    return merged


async def _fetch_baidu_hot() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, headers=_HEADERS) as client:
            response = await client.get(
                "https://top.baidu.com/api/board",
                params={"platform": "wise", "tab": "realtime"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"百度热搜请求失败：{exc}") from exc
    words: list[str] = []
    cards = (payload.get("data") or {}).get("cards") or []
    for card in cards:
        for row in card.get("content") or []:
            for item in row.get("content") or []:
                word = str(item.get("word") or "").strip()
                if word:
                    words.append(word)
                if len(words) >= _MAX_WORDS_PER_SOURCE:
                    return words
    return words


async def _fetch_bilibili_hot() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, headers=_HEADERS) as client:
            response = await client.get(
                "https://app.bilibili.com/x/v2/search/trending/ranking"
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"B站热搜请求失败：{exc}") from exc
    if payload.get("code") != 0:
        return []
    words: list[str] = []
    for item in (payload.get("data") or {}).get("list") or []:
        keyword = str(item.get("keyword") or "").strip()
        if keyword:
            words.append(keyword)
        if len(words) >= _MAX_WORDS_PER_SOURCE:
            break
    return words
