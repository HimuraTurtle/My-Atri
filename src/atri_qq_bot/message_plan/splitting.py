from __future__ import annotations

import re

from ..text_units import _protected_indices, split_sentences


def split_reply_text(text: str, max_chars: int = 44, max_parts: int = 4) -> list[str]:
    text = _compact_reply_text(text)
    if not text:
        return []

    # 模型主动换行 = 想分条发送（说话方式规范：每行一条消息）。
    # 行与行之间绝不合并；行内的多个短句仍可合并成一条。
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    merged: list[str] = []
    for line in lines:
        line_start = len(merged)
        for sentence in _sentence_chunks(line):
            for piece in _hard_wrap(sentence, max_chars):
                if not piece:
                    continue
                if (
                    len(merged) > line_start
                    and len(merged[-1]) + len(piece) <= max_chars
                    and not _ends_with_strong_punctuation(merged[-1])
                ):
                    merged[-1] = f"{merged[-1]}{piece}"
                else:
                    merged.append(piece)

    if len(merged) <= max_parts:
        return merged

    kept = merged[: max_parts - 1]
    tail = "".join(merged[max_parts - 1 :])
    # Generation controls total length. Transport splitting must keep the final
    # thought intact instead of manufacturing an unfinished ellipsis.
    kept.append(tail)
    return kept


def _compact_reply_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text.strip())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _sentence_chunks(text: str) -> list[str]:
    # URL 内的 `?` 等标点不构成句界，共享实现见 text_units。拆条是传输层，
    # 输入是模型的既成输出：切不出任何句子时原样返回，绝不吞内容。
    return split_sentences(text) or [text]


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    protected = _protected_indices(text)
    parts: list[str] = []
    start = 0
    length = len(text)
    while length - start > max_chars:
        # 软标点窗口 [start, start+max_chars)：切点含软标点时恰好切出
        # max_chars 个字符，杜绝旧实现 max_chars+1 的越界。
        cut = -1
        for index in range(start + max_chars - 1, start - 1, -1):
            if index in protected:
                continue
            if text[index] in "，,、 ":
                cut = index + 1
                break
        if cut < 0 or cut - start < max_chars // 2:
            cut = start + max_chars
            # 硬切不能落在 URL 内部：宁可这一条超出字数预算，也要保住
            # 完整链接——链接被拦腰截断后用户拿到的是打不开的碎片。
            while cut < length and cut in protected:
                cut += 1
        part = text[start:cut].strip()
        if part:
            parts.append(part)
        start = cut
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _ends_with_strong_punctuation(text: str) -> bool:
    return text.endswith(("。", "！", "？", "!", "?", "；", ";"))


def _truncate_tail(text: str, limit: int) -> str:
    """Backward-compatible helper that deliberately keeps complete content."""
    return text

# End of module.
