"""漫画命令与 GraphQL 之间的纯逻辑层：章节解析、展示格式化、源选择。"""

from __future__ import annotations

import logging
import math
import re

from .models import Chapter, Manga, Source

LOGGER = logging.getLogger("atri.manga")

try:  # 繁简归一化用于订阅标题模糊匹配；缺库时退化为原样比较。
    from opencc import OpenCC

    _t2s_converter = OpenCC("t2s")
except Exception:  # pragma: no cover - opencc 未安装时的降级
    _t2s_converter = None

STATUS_EMOJI: dict[str, str] = {
    "ONGOING": "连载中",
    "COMPLETED": "已完结",
    "LICENSED": "已授权",
    "PUBLISHING_FINISHED": "已完结",
    "CANCELLED": "已停刊",
    "ON_HIATUS": "休刊中",
    "UNKNOWN": "未知",
}

_CHAPTER_NUM_SUFFIX_RE = re.compile(r"(?:话|話|章)$")


def normalize_zh(text: str) -> str:
    if _t2s_converter is None:
        return str(text or "")
    return _t2s_converter.convert(str(text or ""))


def parse_chapter_number_text(text: str) -> float | None:
    """把 '5'、'第5话'、'第38.5話' 解析为章节号。"""
    cleaned = str(text or "").strip()
    if cleaned.startswith("第"):
        cleaned = cleaned[1:]
    cleaned = _CHAPTER_NUM_SUFFIX_RE.sub("", cleaned).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fmt_chapter_num(num: float) -> int | float | str:
    try:
        if math.isnan(num) or math.isinf(num):
            return "?"
        return int(num) if num == int(num) else num
    except (ValueError, OverflowError):
        return "?"


def fmt_chapter_display(ch: Chapter) -> str:
    name = (ch.name or "").strip()
    if name:
        return name
    return f"第{fmt_chapter_num(ch.chapter_number)}话"


def fmt_chapter_label(ch: Chapter, num_counts: dict[float, int]) -> str:
    num = fmt_chapter_num(ch.chapter_number)
    dup_tag = f" (ID:{ch.id})" if num_counts.get(ch.chapter_number, 0) > 1 else ""
    if ch.name:
        return f"#{num} {ch.name}{dup_tag}"
    return f"#{num}{dup_tag}"


def find_chapters_by_num(chapters: list[Chapter], chapter_num_f: float) -> list[Chapter]:
    return [ch for ch in chapters if abs(ch.chapter_number - chapter_num_f) < 0.01]


def find_chapter_by_id(chapters: list[Chapter], chapter_id: int) -> Chapter | None:
    for ch in chapters:
        if ch.id == chapter_id:
            return ch
    return None


def resolve_chapter(
    chapters: list[Chapter], chapter_num: str, manga_name_or_id: str, cmd: str
) -> tuple[Chapter | None, str | None]:
    norm = chapter_num.replace("：", ":").lower()
    if norm.startswith("id:"):
        try:
            cid = int(norm[3:])
        except ValueError:
            return None, "章节 ID 格式无效。示例: ID:123"
        target = find_chapter_by_id(chapters, cid)
        if target:
            return target, None
        return None, f"未找到 ID 为 {cid} 的章节。"
    chapter_num_f = parse_chapter_number_text(chapter_num)
    if chapter_num_f is None:
        return None, "章节号无效。示例: 1, 38.5, 第5话 或 ID:123"
    matches = find_chapters_by_num(chapters, chapter_num_f)
    if len(matches) == 0:
        return None, f"未找到第 {fmt_chapter_num(chapter_num_f)} 话。"
    if len(matches) == 1:
        return matches[0], None
    ids = " 或 ".join(f"ID:{m.id}" for m in matches)
    return None, (
        f"第 {chapter_num} 话存在多个章节（可能为附录/番外），请使用 ID 指定: 漫画 {cmd} {manga_name_or_id} {ids}"
    )


def match_source_hint(sources: list[Source], hint: str) -> Source | None:
    """把用户给的源提示匹配到具体源；本地源(id=0)会导致搜索崩溃，需排除。"""
    hint = (hint or "").strip().casefold()
    if not hint:
        return None
    for src in sources:
        if str(src.id) == "0":
            continue
        display_core = src.display_name.split(" (")[0].casefold()
        if (
            hint == src.name.casefold()
            or src.name.casefold().startswith(hint)
            or hint == display_core
            or display_core.startswith(hint)
            or hint == src.lang.casefold()
        ):
            return src
    return None


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def select_search_sources(
    sources: list[Source],
    source_hint: str,
    default_source_id,
    max_sources: int,
) -> list[Source]:
    usable = [source for source in sources if str(source.id) != "0"]
    limit = _bounded_int(max_sources, 5, 1, 10)

    hint = source_hint.strip().casefold()
    if hint:
        matches = [
            source
            for source in usable
            if hint in source.name.casefold()
            or hint in source.display_name.casefold()
            or hint == source.lang.casefold()
        ]
        return matches[:limit]

    default_sid = str(default_source_id or "0")
    if default_sid != "0":
        matches = [source for source in usable if str(source.id) == default_sid]
        if matches:
            return matches

    # 优先不同扩展，再补同扩展的语言变体，避免名额被 MangaDex 变体占满。
    primary: list[Source] = []
    variants: list[Source] = []
    seen_names: set[str] = set()
    for source in usable:
        key = source.name.strip().casefold() or source.display_name.strip().casefold()
        if key in seen_names:
            variants.append(source)
            continue
        seen_names.add(key)
        primary.append(source)
    return (primary + variants)[:limit]


def format_manga_status(status: str) -> str:
    return STATUS_EMOJI.get(str(status or "").upper(), "未知")


def summarize_manga_brief(manga: Manga, source_name: str = "") -> str:
    lines = [f"《{manga.title}》"]
    status = format_manga_status(manga.status)
    meta_parts = [status]
    if source_name:
        meta_parts.append(source_name)
    if manga.author:
        meta_parts.append(f"作者: {manga.author}")
    meta_parts.append(f"ID: {manga.id}")
    lines.append("  " + " · ".join(meta_parts))
    if manga.description:
        desc = re.sub(r"\s+", " ", manga.description).strip()
        if len(desc) > 120:
            desc = desc[:120] + "…"
        if desc:
            lines.append(f"  {desc}")
    return "\n".join(lines)
