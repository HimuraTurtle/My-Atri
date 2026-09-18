"""LLM 总结生成：提示词构建、调用、QQ 纯文本后处理与结果缓存。

移植自 astrbot_plugin_bilivideo 的 llm/prompts.py + summarize/orchestrator.py。
原版输出 Markdown 供图片渲染；ATRI 直接发文本，因此提示词改为
「无 Markdown 语法」的纯文本排版（【章节】/· 要点/⏱ 时间戳）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from .client import VideoInfo
from .subtitles import Transcript

LOGGER = logging.getLogger("atri.bilibili")

LLM_TIMEOUT_SECONDS = 180.0
SUMMARY_CACHE_TTL_SECONDS = 21_600  # 6h
SUMMARY_CACHE_MAX = 64

NOTE_STYLES: dict[str, str] = {
    "concise": (
        "【简洁模式】仅提取核心观点和关键结论，每个板块用简短要点概括，"
        "整体控制在 5-8 个要点以内，每个要点一句话。"
    ),
    "detailed": (
        "【详细模式】完整记录视频内容，保留重要的例子、数据和论证过程，"
        "需要尽可能多地记录视频内容。"
    ),
    "professional": (
        "【专业模式】提供深度结构化分析，包含背景概述、核心论点、关键数据和结论建议，"
        "语言正式、逻辑清晰。"
    ),
}

PROMPT_TEMPLATE = """\
你是一个专业的视频总结助手，擅长把视频字幕整理成清晰、有条理的总结。

语言要求：总结使用中文；专有名词、技术术语、品牌名和人名保留英文。

视频标题：{video_title}
UP主：{uploader}
视频标签：{tags}

输出格式要求（非常重要）：
- 输出纯文本，禁止使用任何 Markdown 语法（不要 # 标题、**加粗**、> 引用、- 列表符号）。
- 第一行是标题行，格式：{video_title} | {uploader}
- 用【板块名】的形式分隔不同内容板块，例如【核心观点】【关键数据】【总结】。
- 每个要点单独一行，行首用 · 符号。
- 提及的时间点写成 ⏱ 分:秒 或 ⏱ 时:分:秒（如 ⏱ 04:16）。
- 合理分段，避免单个板块过长。

视频分段字幕（格式：时间 - 内容）：
---
{segment_text}
---

你的任务：根据上面的分段字幕生成结构化总结，遵循以下原则：
1. 完整信息：记录尽可能多的相关细节。
2. 去除无关内容：省略广告、填充词、问候语和不相关的言论。
3. 保留关键细节：重要事实、示例、结论和建议都要保留。
4. 视频中出现的数学公式用文字描述或 LaTeX 记法保留。
{style_instruction}
{summary_instruction}
"""

STYLE_PLACEHOLDER = "{style_instruction}"
SUMMARY_PLACEHOLDER = "{summary_instruction}"

AI_SUMMARY_INSTRUCTION = (
    "最后附一个【AI 总结】板块：用三句话以内概括整个视频最核心的内容。"
)


def format_time(seconds: float) -> str:
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def render_segment_text(segments: list[Any]) -> str:
    return "\n".join(f"{format_time(seg.start)} - {seg.text.strip()}" for seg in segments)


def build_prompt(
    *,
    title: str,
    uploader: str,
    segments: list[Any],
    tags: str = "",
    style: str = "professional",
    enable_summary: bool = True,
) -> str:
    body = PROMPT_TEMPLATE.format(
        video_title=title,
        uploader=uploader,
        tags=tags or "（无）",
        segment_text=render_segment_text(segments),
        style_instruction=NOTE_STYLES.get(style, NOTE_STYLES["professional"]),
        summary_instruction=AI_SUMMARY_INSTRUCTION if enable_summary else "",
    )
    return body


_MD_HEADER_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_QUOTE_RE = re.compile(r"^>\s?", re.MULTILINE)
_MD_CODE_RE = re.compile(r"```[a-zA-Z]*\n?|```")
_MD_LIST_RE = re.compile(r"^[\-\*]\s+", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """兜底剥掉 LLM 偶发输出的 Markdown 语法（QQ 不渲染 Markdown）。"""
    text = _MD_CODE_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_QUOTE_RE.sub("", text)
    text = _MD_LIST_RE.sub("· ", text)
    return text.strip()


def smart_truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    min_keep = int(max_length * 0.7)
    last_break = truncated.rfind("\n\n")
    if last_break > min_keep:
        truncated = truncated[:last_break]
    return (
        truncated
        + "\n\n---\n⚠️ 内容过长，以上为核心部分摘要。"
    )


class BilibiliSummarizer:
    """带 TTL 缓存的总结生成器；LLM 配置复用 ATRI 的 openai_* 字段。"""

    def __init__(self, config: Any) -> None:
        self._base_url = str(getattr(config, "openai_base_url", "")).rstrip("/")
        self._api_key = str(getattr(config, "openai_api_key", "") or "")
        self._model = str(getattr(config, "openai_model", "") or "")
        self._max_note_length = int(getattr(config, "bilibili_max_note_length", 3500))
        self._style = str(getattr(config, "bilibili_note_style", "professional"))
        self._cache: dict[str, tuple[float, str]] = {}

    async def chat(self, prompt: str) -> str:
        if not self._base_url or not self._api_key or not self._model:
            raise RuntimeError("LLM 未配置：缺少 openai_base_url / openai_api_key / openai_model")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4000,
        }
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as http:
            response = await http.post(
                f"{self._base_url}/chat/completions", headers=headers, json=body
            )
            if response.status_code != 200:
                raise RuntimeError(f"LLM 请求失败 HTTP {response.status_code}: {response.text[:200]}")
            payload = response.json()
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not content:
            raise RuntimeError("LLM 返回内容为空")
        return str(content).strip()

    def cached_summary(self, bvid: str) -> str | None:
        entry = self._cache.get(bvid)
        if entry and time.time() - entry[0] <= SUMMARY_CACHE_TTL_SECONDS:
            return entry[1]
        return None

    def remember_summary(self, bvid: str, summary: str) -> None:
        self._cache[bvid] = (time.time(), summary)
        if len(self._cache) > SUMMARY_CACHE_MAX:
            oldest = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest, None)

    async def summarize(
        self, *, video_info: VideoInfo, transcript: Transcript, tags: str = ""
    ) -> str:
        prompt = build_prompt(
            title=video_info.title,
            uploader=video_info.owner_name,
            segments=transcript.segments,
            tags=tags,
            style=self._style,
        )
        markdown_free = strip_markdown(await self.chat(prompt))
        return smart_truncate(markdown_free, self._max_note_length)
