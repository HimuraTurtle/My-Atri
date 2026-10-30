"""论文整理文档：把 search_papers 的检索结果排版成 Word 文档发用户。

结构：检索信息 → 模型综述（可选，由调用方传入）→ 速览列表 → 逐篇
信息卡（标题/作者/年份/期刊/被引/链接/摘要）。元数据全部来自检索
结果原样转写，零编造；docx 生成失败时降级为同结构的 md 文件。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .docx_writer import MiniDocx

LOGGER = logging.getLogger("atri.papers")

_MAX_DIGEST_PAPERS = 8
_MAX_SUMMARY_CHARS = 2000
# 整理文档落盘目录里只保留最新这么多份，防止无限累积。
_MAX_KEPT_FILES = 40
_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_SORT_LABELS = {
    "relevance": "相关度",
    "cited": "被引量",
    "newest": "发表时间",
}


def _safe_name_component(text: str, limit: int = 24) -> str:
    cleaned = _ILLEGAL_FILENAME_RE.sub(" ", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "论文")[:limit]


def _venue_text(paper: dict[str, Any]) -> str:
    venue = str(paper.get("venue") or "").strip()
    return venue if venue else "未知期刊"


def _year_text(paper: dict[str, Any]) -> str:
    year = paper.get("year")
    return str(year) if year else "年份未知"


def _meta_line(paper: dict[str, Any]) -> str:
    return (
        f"年份：{_year_text(paper)} · 期刊：{_venue_text(paper)} · "
        f"被引：{int(paper.get('cited_by_count') or 0)}"
    )


def _cleanup_old_digests(directory: Path, keep: int = _MAX_KEPT_FILES) -> None:
    try:
        files = [p for p in directory.iterdir() if p.is_file()]
    except OSError:
        return
    if len(files) <= keep:
        return
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]:
        try:
            path.unlink()
        except OSError:
            LOGGER.warning("清理旧整理文档失败：%s", path)


def _build_docx(
    path: Path,
    query: str,
    sort_label: str,
    papers: list[dict[str, Any]],
    summary: str,
    stamp: datetime,
) -> None:
    doc = MiniDocx()
    doc.title(f"论文检索整理：{query}")
    doc.meta(
        f"检索时间 {stamp.strftime('%Y-%m-%d %H:%M')} · 按{sort_label}整理 · "
        f"共 {len(papers)} 篇 · 数据来源 OpenAlex"
    )
    if summary:
        doc.heading("综述")
        for paragraph in summary.splitlines():
            paragraph = paragraph.strip()
            if paragraph:
                doc.paragraph(paragraph)
    doc.heading("速览")
    for index, paper in enumerate(papers, start=1):
        doc.bullet(
            f"{index}. {paper.get('title') or '（无标题）'}"
            f"（{_year_text(paper)}，{_venue_text(paper)}，"
            f"被引 {int(paper.get('cited_by_count') or 0)}）"
        )
    doc.heading("论文详情")
    for index, paper in enumerate(papers, start=1):
        doc.heading(f"{index}. {paper.get('title') or '（无标题）'}")
        authors = str(paper.get("authors") or "").strip()
        doc.meta(f"作者：{authors if authors else '未知'}")
        doc.meta(_meta_line(paper))
        url = str(paper.get("url") or "").strip()
        if url:
            doc.link("原文链接", url)
        abstract = str(paper.get("abstract") or "").strip()
        doc.paragraph(f"摘要：{abstract}" if abstract else "摘要：（OpenAlex 未收录摘要）")
    doc.meta("本文档由 ATRI 基于 OpenAlex 元数据自动整理，链接与元数据均为检索原文。")
    doc.save(path)


def _build_markdown(
    path: Path,
    query: str,
    sort_label: str,
    papers: list[dict[str, Any]],
    summary: str,
    stamp: datetime,
) -> None:
    lines: list[str] = [
        f"# 论文检索整理：{query}",
        "",
        f"> 检索时间 {stamp.strftime('%Y-%m-%d %H:%M')} · 按{sort_label}整理 · "
        f"共 {len(papers)} 篇 · 数据来源 OpenAlex",
        "",
    ]
    if summary:
        lines.append("## 综述")
        lines.append("")
        lines.extend(summary.splitlines())
        lines.append("")
    lines.append("## 速览")
    lines.append("")
    for index, paper in enumerate(papers, start=1):
        lines.append(
            f"- {paper.get('title') or '（无标题）'}"
            f"（{_year_text(paper)}，{_venue_text(paper)}，"
            f"被引 {int(paper.get('cited_by_count') or 0)}）"
        )
    lines.append("")
    lines.append("## 论文详情")
    lines.append("")
    for index, paper in enumerate(papers, start=1):
        authors = str(paper.get("authors") or "").strip()
        url = str(paper.get("url") or "").strip()
        abstract = str(paper.get("abstract") or "").strip()
        lines.append(f"### {index}. {paper.get('title') or '（无标题）'}")
        lines.append("")
        lines.append(f"- 作者：{authors if authors else '未知'}")
        lines.append(f"- {_meta_line(paper)}")
        if url:
            lines.append(f"- 原文链接：{url}")
        lines.append(f"- 摘要：{abstract if abstract else '（OpenAlex 未收录摘要）'}")
        lines.append("")
    lines.append("---")
    lines.append("本文档由 ATRI 基于 OpenAlex 元数据自动整理，链接与元数据均为检索原文。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_paper_digest(
    *,
    query: str,
    sort: str,
    papers: list[dict[str, Any]],
    summary: str = "",
    output_dir: Path,
) -> dict[str, Any]:
    """把论文列表排版成 docx（失败降级 md），返回投递描述。"""
    papers = [p for p in papers if isinstance(p, dict)][:_MAX_DIGEST_PAPERS]
    if not papers:
        return {"ok": False, "error": "没有可整理的论文"}
    query = str(query or "").strip() or "论文检索"
    sort_label = _SORT_LABELS.get(str(sort or ""), "相关度")
    summary = str(summary or "").strip()[:_MAX_SUMMARY_CHARS]
    stamp = datetime.now()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = (
        f"论文整理_{_safe_name_component(query)}_{stamp.strftime('%Y%m%d_%H%M%S')}"
    )
    docx_path = output_dir / f"{stem}.docx"
    try:
        _build_docx(docx_path, query, sort_label, papers, summary, stamp)
        format_name = "docx"
        result_path = docx_path
    except Exception as exc:
        LOGGER.exception("论文整理 docx 生成失败，降级为 md")
        md_path = output_dir / f"{stem}.md"
        _build_markdown(md_path, query, sort_label, papers, summary, stamp)
        format_name = "md"
        result_path = md_path
    _cleanup_old_digests(output_dir)
    LOGGER.info(
        "论文整理文档生成完成：format=%s papers=%d query=%.60s",
        format_name,
        len(papers),
        query,
    )
    return {
        "ok": True,
        "format": format_name,
        "path": str(result_path),
        "name": result_path.name,
        "count": len(papers),
    }
