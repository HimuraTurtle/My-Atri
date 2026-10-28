"""受约束的联网搜索工具。

搜索工具返回带来源的文本证据，而不是直接替模型生成答案。不同模式选择
新闻、网页、arXiv/GitHub 等强相关来源，并对结果数量、超时和域名做边界控制。
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlsplit


DEFAULT_SEARCH_TIMEOUT_SECONDS = 6.0
DEFAULT_SEARCH_MAX_RESULTS = 5


async def search_web(arguments: dict[str, Any] | None = None, config: Any | None = None) -> str:
    # 先规范化 query/mode/domains，再选择来源；这样“最新消息”和“研究现状”
    # 走不同的来源集合，减少把普通网页结果误当成前沿研究结论。
    args = arguments or {}
    query = str(args.get("query") or "").strip()
    if not query:
        return "搜索失败：缺少搜索关键词。"

    mode = str(args.get("mode") or "auto").strip().casefold()
    if mode not in {"auto", "web", "news", "research"}:
        mode = "auto"
    domains = _normalize_domains(args.get("domains"))
    effective_query = _domain_scoped_query(query, domains)

    max_results = _positive_int(
        args.get("max_results"),
        int(getattr(config, "web_search_max_results", DEFAULT_SEARCH_MAX_RESULTS) or DEFAULT_SEARCH_MAX_RESULTS),
    )
    max_results = max(1, min(8, max_results))
    timeout = float(
        getattr(config, "web_search_timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS)
        or DEFAULT_SEARCH_TIMEOUT_SECONDS
    )

    errors: list[str] = []
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if mode == "research":
                research_results, research_sources = await _search_research_sources(
                    client,
                    query,
                    max_results,
                    domains,
                )
                if research_results:
                    formatted = format_search_results(
                        query,
                        research_results,
                        source_name=" + ".join(research_sources),
                        mode=mode,
                        domains=domains,
                    )
                    return await _append_primary_source_excerpt(
                        formatted,
                        research_results,
                        config,
                    )
            if _bocha_enabled(config):
                try:
                    bocha_results = await _search_bocha(
                        client,
                        query,
                        mode,
                        max_results,
                        str(getattr(config, "bocha_api_key", "") or ""),
                        domains=domains,
                    )
                    if bocha_results:
                        return format_search_results(
                            query,
                            bocha_results,
                            source_name="博查AI搜索",
                            mode=mode,
                            domains=domains,
                            summary_limit=420,
                        )
                    errors.append("博查AI搜索: 没有结果")
                except Exception as exc:
                    # 博查失败（欠费/超时/网络）不阻断回复，退回免费 RSS 链。
                    errors.append(f"博查AI搜索: {_short_error(exc)}")
            for source_name, url in _search_sources(effective_query, mode):
                try:
                    response = await client.get(url, headers=_request_headers())
                    response.raise_for_status()
                    results = parse_news_rss(response.text, max_results)
                    if domains:
                        results = _filter_results_by_domains(results, domains)
                    if results:
                        return format_search_results(
                            query,
                            results,
                            source_name=source_name,
                            mode=mode,
                            domains=domains,
                        )
                    errors.append(f"{source_name}: 没有结果")
                except Exception as exc:
                    errors.append(f"{source_name}: {_short_error(exc)}")
    except Exception as exc:
        errors.append(_short_error(exc))
    detail = "；".join(errors)[:300] or "未知错误"
    return f"搜索失败：{detail}。不要编造实时信息，可以说明现在没有拿到可靠搜索结果。"


async def _search_research_sources(
    client: Any,
    query: str,
    max_results: int,
    domains: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    sources: list[str] = []
    compact_query = _compact_research_query(query)
    per_source_limit = max(2, (max_results + 1) // 2)
    use_arxiv = not domains or _domain_selected(domains, "arxiv.org")
    use_github = not domains or _domain_selected(domains, "github.com")

    if use_arxiv:
        try:
            response = await client.get(
                "https://export.arxiv.org/api/query",
                params={
                    "search_query": f"all:{compact_query.split()[0]}",
                    "start": 0,
                    "max_results": per_source_limit,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
                headers=_request_headers(),
            )
            response.raise_for_status()
            arxiv_results = parse_arxiv_atom(response.text, per_source_limit)
            if arxiv_results:
                results.extend(arxiv_results)
                sources.append("arXiv")
        except Exception:
            pass

    if use_github:
        try:
            response = await client.get(
                "https://api.github.com/search/repositories",
                params={
                    "q": compact_query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": per_source_limit,
                },
                headers={**_request_headers(), "Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            github_results = parse_github_repository_search(response.json(), per_source_limit)
            if github_results:
                results.extend(github_results)
                sources.append("GitHub")
        except Exception:
            pass

    return _deduplicate_results(results, max_results), sources


def _compact_research_query(query: str) -> str:
    tokens = re.findall(r"[\w.+-]{2,}", str(query or ""), flags=re.UNICODE)
    stopwords = {
        "latest",
        "current",
        "research",
        "limitations",
        "limitation",
        "benchmark",
        "benchmarks",
        "understanding",
        "evaluation",
        "evaluate",
        "study",
        "studies",
        "2025",
        "2026",
    }
    specific = [
        token
        for token in tokens
        if token.casefold() not in stopwords
        and (any(char.isdigit() for char in token) or "-" in token or "." in token)
    ]
    topical = [
        token
        for token in tokens
        if token.casefold() not in stopwords and token not in specific
    ]
    selected = specific[:2] + topical[: max(0, 3 - len(specific[:2]))]
    return " ".join(selected or tokens[:3] or [str(query or "").strip()])


def parse_arxiv_atom(text: str, max_results: int = DEFAULT_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    root = ET.fromstring(text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    results: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", namespace):
        title = _clean_text(entry.findtext("atom:title", default="", namespaces=namespace))
        summary = _clean_text(entry.findtext("atom:summary", default="", namespaces=namespace))
        published = _clean_text(entry.findtext("atom:published", default="", namespaces=namespace))
        url = _clean_text(entry.findtext("atom:id", default="", namespaces=namespace))
        if not title or not url:
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "published_at": published,
                "summary": summary,
                "source_type": "arXiv 预印本（不等于已经形成学界定论）",
            }
        )
        if len(results) >= max(1, max_results):
            break
    return results


def parse_github_repository_search(
    payload: Any,
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
) -> list[dict[str, str]]:
    items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return []
    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("full_name") or item.get("name") or "").strip()
        url = str(item.get("html_url") or "").strip()
        if not title or not url:
            continue
        description = str(item.get("description") or "").strip()
        language = str(item.get("language") or "").strip()
        stars = item.get("stargazers_count")
        details = [description]
        if language:
            details.append(f"主要语言：{language}")
        if isinstance(stars, int):
            details.append(f"Stars：{stars}")
        results.append(
            {
                "title": title,
                "url": url,
                "published_at": str(item.get("updated_at") or ""),
                "summary": "；".join(part for part in details if part),
                "source_type": "GitHub 仓库自述（只代表该仓库作者，不能自动视为官方结论）",
            }
        )
        if len(results) >= max(1, max_results):
            break
    return results


def _google_news_rss_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )


def _bing_news_rss_url(query: str) -> str:
    return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&mkt=zh-CN"


def _bing_web_rss_url(query: str) -> str:
    return f"https://www.bing.com/search?q={quote_plus(query)}&format=rss&mkt=zh-CN"


def parse_bing_news_rss(text: str, max_results: int = DEFAULT_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    return parse_news_rss(text, max_results)


def parse_news_rss(text: str, max_results: int = DEFAULT_SEARCH_MAX_RESULTS) -> list[dict[str, str]]:
    root = ET.fromstring(text)
    results: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        title = _clean_text(item.findtext("title") or "")
        link = _clean_text(item.findtext("link") or "")
        published_at = _clean_text(item.findtext("pubDate") or "")
        summary = _clean_text(item.findtext("description") or "")
        if not title and not summary:
            continue
        results.append(
            {
                "title": title,
                "url": link,
                "published_at": published_at,
                "summary": summary,
            }
        )
        if len(results) >= max(1, max_results):
            break
    return results


def _bocha_enabled(config: Any) -> bool:
    provider = str(getattr(config, "search_provider", "auto") or "auto").strip().casefold()
    if provider not in {"auto", "bocha"}:
        return False
    return bool(str(getattr(config, "bocha_api_key", "") or "").strip())


# 博查 freshness 分级：时敏词越强窗口越窄，避免"最近/今天"类查询
# 拿到陈旧资料后模型只能说"信息滞后"。
_FRESHNESS_ONE_DAY_RE = re.compile(r"今天|今日|现在|此刻|刚刚|昨晚|昨夜|今晚|实时")
_FRESHNESS_ONE_WEEK_RE = re.compile(r"最近|最新|本周|这周|近况|近日|近来|新闻|时事|热点|上新|新出|新番")
_FRESHNESS_ONE_YEAR_RE = re.compile(r"今年|去年|年内")


def _bocha_freshness(mode: str, query: str) -> str | None:
    """按查询词的时间敏感度选博查时效窗口；普通网页查询不加过滤。"""

    text = str(query or "")
    if _FRESHNESS_ONE_DAY_RE.search(text):
        return "oneDay"
    if mode != "news":
        return None
    if _FRESHNESS_ONE_WEEK_RE.search(text):
        return "oneWeek"
    if _FRESHNESS_ONE_YEAR_RE.search(text):
        return "oneYear"
    return "oneMonth"


async def _search_bocha(
    client: Any,
    query: str,
    mode: str,
    max_results: int,
    api_key: str,
    domains: list[str] | None = None,
) -> list[dict[str, str]]:
    """博查 Web Search API：语义检索 + AI 正文摘要，中文质量远高于 RSS。

    失败直接抛异常，由调用方退回免费 RSS 链，保证搜索能力永不中断。
    """

    payload: dict[str, Any] = {
        "query": query,
        "summary": True,
        "count": max(1, min(8, max_results)),
        "page": 1,
    }
    if domains:
        payload["include"] = "|".join(domains)
    freshness = _bocha_freshness(mode, query)
    if freshness:
        payload["freshness"] = freshness
    response = await client.post(
        "https://api.bochaai.com/v1/web-search",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    body = response.json()
    # 博查把结果包在 data 字段下；兼容直接顶层的旧格式。
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    pages = data.get("webPages") or {}
    values = pages.get("value") or []
    results: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        summary = str(item.get("summary") or item.get("snippet") or "").strip()
        results.append(
            {
                "title": title,
                "url": url,
                "published_at": str(item.get("datePublished") or ""),
                "summary": summary,
                "source_type": (
                    "博查AI提取的网页正文摘要，可直接作为证据使用"
                    if summary
                    else "一般网页/媒体摘要，需结合原文判断"
                ),
            }
        )
        if len(results) >= max(1, max_results):
            break
    return results


_QUERY_FILLER = re.compile(
    r"(是什么梗|是什么意思|什么意思|是什么|啥意思|什么梗|是谁|什么是|哪个|哪些|"
    r"什么|怎么|如何|为什么|推荐|最新|攻略|测评|介绍|解释|出处|出自|来源|"
    r"好不好|怎么样|好用|最好|最强|比较|比较好|一下|请问|帮我|帮我查|查一下)"
)
_CJK_RANGE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z]")


def _query_key_terms(query: str) -> list[str]:
    """从查询词里去掉疑问/修饰词，留下实体关键词（用于相关性排序）。"""

    cleaned = _QUERY_FILLER.sub(" ", str(query or ""))
    terms: list[str] = []
    for token in re.split(r"[\s,，。.、;；:：!！?？~～\"'()（）\[\]【】]+", cleaned):
        token = token.strip().casefold()
        if not token:
            continue
        cjk_count = len(_CJK_RANGE.findall(token))
        ascii_count = len(_ASCII_WORD.findall(token))
        if cjk_count >= 2 or ascii_count >= 3:
            terms.append(token)
    return terms


def _rank_by_relevance(
    results: list[dict[str, str]],
    key_terms: list[str],
) -> list[tuple[dict[str, str], bool]]:
    """标题包含查询实体词的结果排前面，并标记相关性。

    搜索引擎常对相似名做模糊匹配（搜"樱羽艾玛"返回"樱木茉莉"），
    显式排序加标注能防止模型把撞名结果当成答案。
    """

    ranked: list[tuple[int, int, dict[str, str], bool]] = []
    for order, result in enumerate(results):
        title = str(result.get("title") or "").casefold()
        matched = any(term in title for term in key_terms)
        ranked.append((0 if matched else 1, order, result, matched))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [(item[2], item[3]) for item in ranked]


def format_search_results(
    query: str,
    results: list[dict[str, str]],
    source_name: str = "新闻 RSS",
    mode: str = "auto",
    domains: list[str] | None = None,
    summary_limit: int = 220,
) -> str:
    searched_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    if not results:
        return (
            f"搜索时间：{searched_at} Asia/Shanghai\n"
            f"搜索关键词：{query}\n"
            "没有搜索到明确结果。不要编造实时信息，可以说明没拿到可靠来源。"
        )

    key_terms = _query_key_terms(query)
    ranked_results = _rank_by_relevance(results, key_terms)
    has_low_relevance = any(not matched for _result, matched in ranked_results)

    lines = [
        f"搜索时间：{searched_at} Asia/Shanghai",
        f"搜索关键词：{query}",
        f"搜索类型：{mode}",
        f"聚合来源：{source_name}",
        "搜索结果如下。实时信息要结合发布时间判断时效性；一般网页可用于核验名称、出处和事实：",
    ]
    if has_low_relevance:
        lines.append(
            "注意：已按与查询实体的相关性排序；标注「相关性：低」的结果标题并不包含"
            "查询关键词，很可能只是撞名或跑题，不要把它当成问题的答案。"
        )
    if domains:
        lines.insert(3, f"优先网站：{', '.join(domains)}")
    for index, (result, matched) in enumerate(ranked_results, start=1):
        title = result.get("title") or "无标题"
        published_at = result.get("published_at") or "未标明"
        summary = _shorten(result.get("summary") or "无摘要", max(80, int(summary_limit)))
        url = result.get("url") or "无链接"
        source_type = result.get("source_type") or "一般网页/媒体摘要，需结合原文判断"
        relevance = "高（标题含查询关键词）" if matched else "低（标题不含查询关键词，谨慎采用）"
        lines.append(
            f"{index}. 标题：{title}\n"
            f"   相关性：{relevance}\n"
            f"   来源性质：{source_type}\n"
            f"   发布时间：{published_at}\n"
            f"   摘要：{summary}\n"
            f"   URL：{url}"
        )
    return "\n".join(lines)


def _search_sources(query: str, mode: str) -> tuple[tuple[str, str], ...]:
    web = ("Bing Web", _bing_web_rss_url(query))
    google_news = ("Google News", _google_news_rss_url(query))
    bing_news = ("Bing News", _bing_news_rss_url(query))
    if mode == "news":
        return google_news, bing_news, web
    if mode in {"web", "research"}:
        return web, google_news, bing_news
    return web, google_news, bing_news


def _normalize_domains(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    domains: list[str] = []
    for raw in value[:8]:
        candidate = str(raw or "").strip().casefold()
        if "://" in candidate:
            candidate = urlsplit(candidate).hostname or ""
        candidate = candidate.strip(". ")
        if not re.fullmatch(r"[a-z0-9.-]{3,253}", candidate):
            continue
        if ".." in candidate or candidate.startswith("-") or candidate.endswith("-"):
            continue
        if candidate not in domains:
            domains.append(candidate)
    return domains


def _domain_scoped_query(query: str, domains: list[str]) -> str:
    if not domains:
        return query
    scoped = " OR ".join(f"site:{domain}" for domain in domains)
    return f"{query} ({scoped})"


def _filter_results_by_domains(
    results: list[dict[str, str]],
    domains: list[str],
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for result in results:
        hostname = (urlsplit(str(result.get("url") or "")).hostname or "").casefold().rstrip(".")
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            selected.append(result)
    return selected


def _domain_selected(domains: list[str], expected: str) -> bool:
    return any(domain == expected or domain.endswith(f".{expected}") for domain in domains)


def _deduplicate_results(
    results: list[dict[str, str]],
    max_results: int,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for result in results:
        url = str(result.get("url") or "").strip()
        title = str(result.get("title") or "").strip()
        key = (url or title).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(result)
        if len(selected) >= max(1, max_results):
            break
    return selected


async def _append_primary_source_excerpt(
    formatted_results: str,
    results: list[dict[str, str]],
    config: Any,
) -> str:
    if not results:
        return formatted_results
    url = str(results[0].get("url") or "").strip()
    if not url:
        return formatted_results
    try:
        from .web_page_tool import open_web_page

        page = await open_web_page({"url": url, "max_chars": 2800}, config)
    except Exception:
        return formatted_results
    if page.startswith("网页读取失败"):
        return formatted_results
    return (
        f"{formatted_results}\n\n"
        "已自动读取排名最高的原始来源，下面正文证据优先于搜索摘要：\n"
        f"{page}"
    )


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, parsed)


def _clean_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _shorten(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = re.sub(r"\s+", " ", text)
    return _shorten(text, 160)


def _request_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
        )
    }
