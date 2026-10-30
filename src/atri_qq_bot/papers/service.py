"""学术论文检索（OpenAlex）：为聊天模型提供文献查找能力。

单一免费数据源 OpenAlex（api.openalex.org，免 key）：全学科期刊文献
与预印本元数据，覆盖标题、作者、年份、期刊、被引数、DOI 链接与摘要。
摘要以倒排索引（word -> [positions]）存储，需按位置重建。

排序契约：OpenAlex 的 sort 参数会覆盖 search 的相关度排序，导致
返回全网高被引却跑题的文献；因此请求永远按相关度取前 _FETCH_LIMIT
篇，cited/newest 排序只在相关集合内做——保证结果既相关又按需排列。

降级契约：接口失败或无结果时返回明确的中文错误说明（由模型转达），
绝不编造论文条目。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.papers")

_OPENALEX_BASE = "https://api.openalex.org/works"
_MAX_QUERY_CHARS = 300
_MAX_ABSTRACT_CHARS = 240
_MAX_LIMIT = 8
# 相关度候选池大小：cited/newest 在该池内重排，池必须大于等于最大 limit。
_FETCH_LIMIT = 50
_SORTS = {"relevance", "newest", "cited"}
# 最近一次检索结果的会话级缓存：compile_paper_digest 从这里取素材。
_LAST_SEARCH_TTL_SECONDS = 1800.0
_MAX_REMEMBERED_CONVERSATIONS = 64


def _rebuild_abstract(inverted: Any) -> str:
    """把 OpenAlex 的倒排摘要索引重建为连续文本。"""
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes or []:
            if isinstance(index, int) and isinstance(word, str):
                positions.append((index, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def _author_names(authorships: list[Any]) -> str:
    names: list[str] = []
    total = 0
    for item in authorships or []:
        if not isinstance(item, dict):
            continue
        total += 1
        if len(names) >= 3:
            continue
        author = item.get("author") or {}
        name = str(author.get("display_name") or "").strip()
        if name:
            names.append(name)
    if total > 3:
        return ", ".join(names) + " et al."
    return ", ".join(names)


def _venue_name(work: dict[str, Any]) -> str:
    location = work.get("primary_location") or {}
    if not isinstance(location, dict):
        return ""
    source = location.get("source") or {}
    if not isinstance(source, dict):
        return ""
    return str(source.get("display_name") or "").strip()


def _paper_url(work: dict[str, Any]) -> str:
    """论文网址：DOI 优先（直达出版商页），无 DOI 退回 OpenAlex 页。"""
    doi = str(work.get("doi") or "").strip()
    if doi.startswith("http"):
        return doi
    return str(work.get("id") or "").strip()


def _to_paper(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(work.get("display_name") or "").strip(),
        "authors": _author_names(work.get("authorships") or []),
        "year": work.get("publication_year"),
        "venue": _venue_name(work),
        "cited_by_count": int(work.get("cited_by_count") or 0),
        "type": str(work.get("type") or ""),
        "doi": str(work.get("doi") or ""),
        "url": _paper_url(work),
        "abstract": _rebuild_abstract(work.get("abstract_inverted_index"))[
            :_MAX_ABSTRACT_CHARS
        ],
    }


class PapersService:
    """OpenAlex 文献检索：只读 HTTP 查询。

    服务自身只带两块轻状态：每会话最近一次成功检索的结果（供
    compile_paper_digest 整理成文档）与待投递文档队列（OneBot 层
    在文字回复发出后消费，与 minimax/anima 同一模式）。
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._last_searches: dict[str, tuple[float, dict[str, Any]]] = {}
        self._digest_deliveries: dict[str, list[Path]] = {}

    def remember_search(self, conversation_id: str, payload: dict[str, Any]) -> None:
        """记录会话最近一次成功检索，供稍后整理成文档。"""
        if not payload.get("ok") or not payload.get("papers"):
            return
        self._last_searches[str(conversation_id)] = (time.monotonic(), dict(payload))
        if len(self._last_searches) > _MAX_REMEMBERED_CONVERSATIONS:
            oldest = sorted(self._last_searches.items(), key=lambda kv: kv[1][0])
            for key, _ in oldest[: len(self._last_searches) - _MAX_REMEMBERED_CONVERSATIONS]:
                self._last_searches.pop(key, None)

    def last_search(self, conversation_id: str) -> dict[str, Any] | None:
        entry = self._last_searches.get(str(conversation_id))
        if entry is None:
            return None
        stamp, payload = entry
        if time.monotonic() - stamp > _LAST_SEARCH_TTL_SECONDS:
            self._last_searches.pop(str(conversation_id), None)
            return None
        return payload

    def queue_digest_delivery(self, conversation_id: str, path: Path) -> None:
        queue = self._digest_deliveries.setdefault(str(conversation_id), [])
        queue.append(Path(path))

    def consume_digest_deliveries(self, conversation_id: str) -> list[Path]:
        return self._digest_deliveries.pop(str(conversation_id), [])

    async def search(
        self, query: str, limit: int = 5, sort: str = "relevance"
    ) -> dict[str, Any]:
        query = str(query or "").strip()[:_MAX_QUERY_CHARS]
        if not query:
            return {"ok": False, "error": "检索词为空"}
        limit = max(1, min(_MAX_LIMIT, int(limit or 5)))
        if str(sort or "").strip().lower() not in _SORTS:
            sort = "relevance"
        params: dict[str, str] = {
            "search": query,
            "per-page": str(_FETCH_LIMIT),
        }
        timeout = max(
            10.0,
            min(60.0, float(getattr(self._config, "papers_timeout_seconds", 30.0) or 30.0)),
        )
        try:
            data = await self._fetch(params, timeout)
        except Exception as exc:
            LOGGER.warning("论文检索失败（%s）：%s", query, exc)
            return {"ok": False, "error": f"OpenAlex 接口调用失败：{exc}"}
        results = [
            work
            for work in (data.get("results") or [])
            if isinstance(work, dict)
            and str(work.get("display_name") or "").strip()
        ]
        if sort == "cited":
            results.sort(key=lambda w: int(w.get("cited_by_count") or 0), reverse=True)
        elif sort == "newest":
            results.sort(key=lambda w: str(w.get("publication_date") or ""), reverse=True)
        papers = [_to_paper(work) for work in results[:limit]]
        LOGGER.info(
            "论文检索完成：query=%.60s sort=%s count=%d", query, sort, len(papers)
        )
        return {"ok": True, "query": query, "sort": sort, "count": len(papers), "papers": papers}

    async def _fetch(self, params: dict[str, str], timeout: float) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(_OPENALEX_BASE, params=params)
            response.raise_for_status()
            return response.json()
