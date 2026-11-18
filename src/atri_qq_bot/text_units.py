"""URL 感知的文本切分与计数工具。

拆条、问句修剪、未完成判定都基于标点扫描；URL 内部恰好含有 `?`、`()`、
`。`（罕见）等标点时，这些护栏会把链接拦腰截断、当成问句丢弃或误判括号
失衡。第一性原理：URL 是原子 token——句界、问号与括号配对的判定必须跳过
URL 内部。本模块是这套判定的唯一实现，供传输层拆条（message_plan）与
人格层修剪（persona）共同消费，避免两处正则各自漂移。
"""

from __future__ import annotations

import re

# RFC 3986 保留 + 未保留字符集：URL 里合法的 ASCII，天然不含 CJK 与全角标点，
# 因此中文句子里的 URL 会停在汉字前，句尾的 。也不会被吞进 URL。
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

_SENTENCE_TERMINATORS = "。！？!?；;\n"
# 句号后紧跟的收尾引号/括号属于前一句：「他说“好吗？”」切分时引号不能
# 落到下一句，否则丢弃问句会制造引号失衡。
_TRAILING_CLOSERS = "”』」）)】]"
# 只有句末标点（含连续问号/省略号与收尾引号）而没有实义字符的碎片段
# 不构成一句话；emoji 等非标点符号算实义字符，不能丢。
_NON_SENTENCE_CHARS = re.compile(r"^[。！？!?；;\n…“”『」（）)【】\s]+$")


def url_spans(text: str) -> list[tuple[int, int]]:
    """返回 text 中 URL 的 [start, end) 区间列表。"""
    return [(match.start(), match.end()) for match in _URL_RE.finditer(text or "")]


def _protected_indices(text: str) -> frozenset[int]:
    protected: set[int] = set()
    for start, end in url_spans(text):
        protected.update(range(start, end))
    return frozenset(protected)


def split_sentences(text: str) -> list[str]:
    """按句末标点切句；URL 内部的标点不构成句界。

    - 同字符连续标点（「。。。」「？？？」）与紧随的收尾引号并入前一句；
    - 纯标点碎片段（无实义字符）丢弃，与旧正则行为一致。
    """
    text = str(text or "")
    if not text:
        return []
    protected = _protected_indices(text)
    chunks: list[str] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if index in protected or char not in _SENTENCE_TERMINATORS:
            index += 1
            continue
        end = index + 1
        while end < length and text[end] == char:
            end += 1
        while end < length and text[end] in _TRAILING_CLOSERS and end not in protected:
            end += 1
        chunks.append(text[start:end])
        start = end
        index = end
    if start < length:
        chunks.append(text[start:])
    return [
        chunk.strip()
        for chunk in chunks
        if chunk.strip() and not _NON_SENTENCE_CHARS.fullmatch(chunk)
    ]


def has_question_mark(text: str) -> bool:
    """是否存在 URL 之外的问号（半角/全角）。"""
    text = str(text or "")
    if "?" not in text and "？" not in text:
        return False
    protected = _protected_indices(text)
    return any(
        char in "?？" and index not in protected
        for index, char in enumerate(text)
    )


def count_questions(text: str) -> int:
    """按“问号串”计数：连续问号（强调式「？？？」）算一个问题，URL 内的问号不算。"""
    text = str(text or "")
    if "?" not in text and "？" not in text:
        return 0
    protected = _protected_indices(text)
    count = 0
    in_run = False
    for index, char in enumerate(text):
        is_question = char in "?？" and index not in protected
        if is_question and not in_run:
            count += 1
        in_run = is_question
    return count


def mask_urls(text: str, fill: str = "\u2003") -> str:
    """把 URL 原位替换为等长占位符，供括号配对等逐字符统计使用。"""
    text = str(text or "")
    spans = url_spans(text)
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = fill
    return "".join(chars)


def last_sentence_boundary(text: str) -> int:
    """最后一个位于 URL 之外的句末标点（。！？!?）下标；没有则返回 -1。"""
    text = str(text or "")
    protected = _protected_indices(text)
    for index in range(len(text) - 1, -1, -1):
        if index in protected:
            continue
        if text[index] in "。！？!?":
            return index
    return -1
