"""论文题录概要的确定性触发解析。

用户在消息里粘贴文献题录（带 DOI / arXiv 链接或编号）并表达
「概要 / 整理 / 文档」输出意图时，不依赖聊天模型是否调用工具，
直接按标识符从 OpenAlex 抓取该篇元数据生成概要文档——模型
「承诺发文档却不调工具」的三次实测失败（口头「文档随后就到」
从未兑现）证明文档交付必须是系统不变量，不能依赖模型自觉。
"""

from __future__ import annotations

import re

# DOI：10.前缀 + 注册号/后缀；结尾排除常见中英文标点，避免把句号
# 括号等吞进标识符。
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s，。；、）)】\]\"']+)")
# arXiv 编号：arXiv:2301.12345（v2 可选）或裸 2301.12345 不认（误报高）。
_ARXIV_RE = re.compile(r"arxiv[:：]?\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
# 只认能被 OpenAlex 反解的域名：doi.org 与 arxiv.org。
_URL_RE = re.compile(r"https?://[^\s，。；、）)】\]\"']+")
_KNOWN_URL_HOSTS = ("doi.org", "arxiv.org")

_INTENT_RE = re.compile(r"(概要|总结|整理|文档|综述)")
# 否定词：否定词出现后约 6 个字符内的意图词（如「不用整理文档」的
# 「整理」「文档」）都属于同一次否定，不能只看紧邻的前一个词。
_NEGATION_RE = re.compile(r"(不用|不需要|不要|别|先不)")
_NEGATION_COVER_CHARS = 6


def _intent_is_affirmed(text: str) -> bool:
    negations = list(_NEGATION_RE.finditer(text))
    for match in _INTENT_RE.finditer(text):
        covered = any(
            neg.start() <= match.start() < neg.end() + _NEGATION_COVER_CHARS
            for neg in negations
        )
        if not covered:
            return True
    return False


def _canonical_identifier(text: str) -> str | None:
    """从消息中提取 OpenAlex 可反解的文献标识（规范成完整 URL）。"""
    doi = _DOI_RE.search(text)
    if doi:
        # 去掉尾部误吞的 ASCII 点/逗号（正则已排除全角标点）。
        value = doi.group(1).rstrip(".,;")
        return f"https://doi.org/{value}"
    for url in _URL_RE.finditer(text):
        value = url.group(0).rstrip(".,;")
        lowered = value.lower()
        if any(host in lowered for host in _KNOWN_URL_HOSTS):
            return value
    arxiv = _ARXIV_RE.search(text)
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv.group(1)}"
    return None


def parse_citation_digest_request(text: str) -> str | None:
    """消息表达「论文题录概要输出」意图时返回规范文献标识，否则 None。"""
    text = str(text or "").strip()
    if not text or not _intent_is_affirmed(text):
        return None
    return _canonical_identifier(text)

# ---- 一般论文检索意图（无 DOI/arXiv 标识）----

_PAPER_SEARCH_PREFIXES = (
    "帮我", "给我", "麻烦", "请你", "请", "我想", "想", "要",
    "写一篇", "写一下", "整理一下", "总结一下", "查一下", "搜一下", "找一下",
    "关于", "有关于", "最近", "最新", "相关", "一下",
)
_PAPER_SEARCH_VERB = r"(?:查|找|搜|检索|搜索|看看|了解|推荐|介绍|整理|总结|有什么)"


def _clean_paper_query(raw: str) -> str:
    text = re.sub(r"[，。！？、；：,.!?;:]+", " ", str(raw or "")).strip()
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PAPER_SEARCH_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
    text = re.sub(
        r"(的最新|的相关|的研究|的论文|的文献|方面|领域|方向|相关)$", "", text
    ).strip()
    text = text.strip("的").strip()
    return text if 2 <= len(text) <= 80 else ""


def parse_paper_search_request(text: str) -> str | None:
    """从消息中解析「一般论文检索」意图并返回检索词，否则 None。

    这是论文查询的确定性兜底：意图明确时（如「帮我查图神经网络的论文」、
    「大模型综述」）不经聊天模型直接检索，避免模型漏调 search_papers。
    带 DOI/arXiv 的消息由 parse_citation_digest_request 处理，这里不触发。
    """
    normalized = " ".join(str(text or "").split()).strip()
    if len(normalized) < 3:
        return None
    match = re.search(
        _PAPER_SEARCH_VERB
        + r"(?:一下|几篇|一些|最近|最新|相关|关于)?"
        + r"(.{2,60}?)"
        + r"(?:的|方面|领域|方向|相关)?"
        + r"(?:论文|文献|paper|papers)",
        normalized,
        re.IGNORECASE,
    )
    if match:
        query = _clean_paper_query(match.group(1))
        if query:
            return query
    match = re.search(
        r"(.{2,60}?)(?:的)?(?:研究进展|研究现状|文献综述|综述|survey|review)",
        normalized,
        re.IGNORECASE,
    )
    if match:
        query = _clean_paper_query(match.group(1))
        if query:
            return query
    return None

