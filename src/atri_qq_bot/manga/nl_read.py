"""自然语言看漫画请求的确定性解析。

实测（2026-11-12 reply-events）聊天模型对「atri我想看死神BLEACH的
第233话」这类请求连续口头承诺「我去找找」「这就去找」却从不调用
漫画工具（tool_calls 为空），漫画永远不送达。这里把带明确标题与
话数的自然语言请求解析成「漫画 阅读」命令参数，交给既有确定性
管道执行——是否送达不再依赖模型行为。
"""

from __future__ import annotations

import re

# 意图短语 + 标题（不含常见标点）+ 可选「的」+ 第N话/回/章/节/集。
# 要求同时出现意图、标题与话数三个要素，闲聊（「看两小时漫画」
# 「这漫画有女主角吗」）不会命中。
_NL_READ_RE = re.compile(
    r"(?P<intent>我想看|我要看|想看|要看|给我看|给我发|发我|看下|看一下|来点)"
    r"(?P<title>[^，。,.!?！？~\n]{1,40}?)"
    r"(?:的)?第(?P<num>[0-9一二三四五六七八九十百零两]+)\s*(?:话|回|章|节|集)"
)
# 意图词直接前缀是否为否定（「不想看」「别看」）。
_NEGATION_TAILS = ("不", "别", "没")
# 标题是这类泛称时无法定位具体作品，不触发（放行给聊天模型）。
_GENERIC_TITLES = {"漫画", "动画", "动漫", "番", "番剧", "本子", "新番"}
_TITLE_QUOTES = "《》「」『』【】〔〕[]"

_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_to_int(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        value = int(text)
        return value if value > 0 else None
    if not text:
        return None
    total = 0
    num = 0
    for ch in text:
        if ch in _DIGITS:
            num = _DIGITS[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return None
    value = total + num
    return value if value > 0 else None


def parse_read_request(text: str) -> tuple[str, int] | None:
    """解析「我想看《标题》第N话」类请求，返回 (标题, 话数) 或 None。"""
    text = str(text or "")
    for match in _NL_READ_RE.finditer(text):
        prefix = text[max(0, match.start() - 1) : match.start()]
        if prefix.endswith(_NEGATION_TAILS):
            continue
        title = match.group("title").strip().strip(_TITLE_QUOTES).strip()
        # 标题里的空白压缩掉，保证合成命令按单 token 解析
        # （「漫画 阅读 <名> <话数>」的命令格式约束）。
        title = re.sub(r"\s+", "", title)
        if len(title) < 2 or title in _GENERIC_TITLES:
            continue
        num = _chinese_to_int(match.group("num"))
        if num is None:
            continue
        return title, num
    return None
