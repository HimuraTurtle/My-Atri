"""问梗即查：用户询问某个表达的含义时，先联网搜索再回答。

模型对"我是不是认识这个词"并不可靠——17:16 的实测里它自以为认识
"简易长矛"就直接瞎猜。这里用确定性触发代替模型自觉：命中询问句式
且该词不在梗库时，由代码直接调用 search_web，把证据注入上下文。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .store import _normalise_term, load_meme_store


LOGGER = logging.getLogger("atri.memes.inquiry")

_INQUIRY_PATTERNS = (
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是什么梗",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是啥梗",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)什么梗",
    r"知道(?P<term>[^，。？！,?!~、\s吗]{1,16}?)(?:这个梗|这个梗儿|梗)",
    r"你知道(?P<term>[^，。？！,?!~、\s吗]{1,16}?)吗",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是什么意思",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是啥意思",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)啥意思",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)什么意思",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是谁",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是什么",
    r"(?P<term>[^，。？！,?!~、\s]{1,16}?)是啥",
    r"什么是(?P<term>[^，。？！,?!~、\s吗呢吧呀咯呗]{1,16})",
)

# 询问对象里出现这些字，多半是代词/指代，不是在问一个具体词。
_TERM_BLOCK_CHARS = set("我你他她它这那啥咋怎什为呀吧呗咯")

# 泛指词当询问对象没有检索价值（"主角是谁"搜不出有效内容）。
_TERM_BLOCK_WORDS = frozenset(
    {"主角", "女主", "男主", "配角", "坏人", "好人", "意思", "东西", "问题", "区别", "原因"}
)

# 游戏/动漫垂直站：群聊语境里陌生的名词多半来自这两个圈子，
# 先在这些站里找，找不到再全网搜。博查 include 最多 20 个域名。
_VERTICAL_DOMAINS = (
    "moegirl.org.cn",
    "biligame.com",
    "bangumi.tv",
    "huijiwiki.com",
    "fandom.com",
    "gamersky.com",
    "3dmgame.com",
    "gcores.com",
)


def extract_inquiry_term(text: str) -> str:
    """从用户消息里提取被询问的表达；没有命中返回空串。"""

    import re

    value = str(text or "").strip()
    if not value:
        return ""
    for pattern in _INQUIRY_PATTERNS:
        match = re.search(pattern, value)
        if not match:
            continue
        term = (match.group("term") or "").strip()
        if not (2 <= len(term) <= 16):
            continue
        if term.isdigit():
            continue
        if term in _TERM_BLOCK_WORDS:
            continue
        if any(char in _TERM_BLOCK_CHARS for char in term):
            continue
        return term
    return ""


_CACHE_TTL_SECONDS = 1800.0
_CACHE_LIMIT = 24
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_LOCK = threading.Lock()


async def maybe_meme_inquiry_context(user_text: str, config: Any) -> str:
    """用户在问一个梗库之外的表达时，联网搜索并返回可注入的证据上下文。

    名词类询问优先在游戏/动漫垂直站里找（群聊里的陌生词多半来自这两个
    圈子），找不到再全网搜；垂直结果的第一条是热度最高的匹配，作为
    话题锚点提示给模型。
    """

    if not bool(getattr(config, "web_search_enabled", True)):
        return ""
    term = extract_inquiry_term(user_text)
    if not term:
        return ""
    if _term_in_store(term):
        return ""
    cached = _cache_get(term)
    if cached is not None:
        return cached
    from ..llm_tools.web_search_tool import search_web

    vertical_results = ""
    try:
        vertical_results = await search_web(
            {
                "query": term,
                "domains": list(_VERTICAL_DOMAINS),
                "max_results": 3,
                "mode": "web",
            },
            config,
        )
    except Exception as exc:
        LOGGER.warning("问词垂直搜索失败：term=%s error=%s", term, exc)
    if _has_search_results(vertical_results):
        context = (
            f"用户在询问「{term}」这个表达。以下是刚刚在游戏/动漫垂直站点"
            "（萌娘百科、B站游戏wiki、Bangumi、游民星空等）检索到的真实资料：\n"
            f"{vertical_results}\n"
            "基于以上证据回应：结果的第一条是热度最高的匹配，优先以它为话题——"
            "说清这个词指的是哪个游戏/动漫里的什么、热度如何，像你本来就懂一样自然接话；"
            "资料和它无关时坦诚说不太了解，不要凭印象编造。"
        )
        _cache_put(term, context)
        return context

    general_results = ""
    try:
        general_results = await search_web(
            {"query": f"{term} 网络梗 什么意思", "max_results": 4, "mode": "web"},
            config,
        )
    except Exception as exc:
        LOGGER.warning("问词全网搜索失败：term=%s error=%s", term, exc)
        return ""
    if not _has_search_results(general_results):
        _cache_put(term, "")
        return ""
    context = (
        f"用户在询问「{term}」这个表达。以下是刚刚联网搜索到的真实资料：\n"
        f"{general_results}\n"
        "基于以上证据回应：如果资料表明它是网络梗或流行语，就自然地接住这个话题；"
        "资料和它无关时坦诚说不太了解，不要凭印象编造含义。"
    )
    _cache_put(term, context)
    return context


def _has_search_results(text: str) -> bool:
    value = str(text or "").strip()
    if not value or value.startswith("搜索失败"):
        return False
    return "搜索结果如下" in value and "没有搜索到明确结果" not in value


def _term_in_store(term: str) -> bool:
    key = _normalise_term(term)
    if not key:
        return False
    for entry in load_meme_store().get("entries", []):
        if _normalise_term(str(entry.get("term") or "")) == key:
            return True
    return False


def _cache_get(term: str) -> str | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(term)
        if hit is None:
            return None
        moment, value = hit
        if time.time() - moment > _CACHE_TTL_SECONDS:
            _CACHE.pop(term, None)
            return None
        return value


def _cache_put(term: str, value: str) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
            _CACHE.pop(oldest, None)
        _CACHE[term] = (time.time(), value)
