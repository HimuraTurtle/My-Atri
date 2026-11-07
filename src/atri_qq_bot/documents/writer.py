"""通用文档输出：把聊天中整理的正文渲染成 Word 文档发用户。

与论文整理文档（papers/digest.py）共用同一个零依赖 MiniDocx 写入器
（跨模块借用，不复制代码）；输入是模型基于对话已掌握内容写的轻量
Markdown——「# / ##」小节标题、「- / *」列表项、「> 」备注行、
单独一行的网址或 [文字](网址) 渲染成可点击链接，其余按正文段落。
系统只做排版落盘，不改动内容本身；docx 生成失败时降级为 md 文件。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..papers.docx_writer import MiniDocx

LOGGER = logging.getLogger("atri.documents")

_MAX_TITLE_CHARS = 60
_MAX_CONTENT_CHARS = 12000
# 输出目录里只保留最新这么多份，防止无限累积（与论文整理同策略）。
_MAX_KEPT_FILES = 40
_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_URL_RE = re.compile(r"^https?://\S+$")
_MARKDOWN_LINK_RE = re.compile(r"^\[([^\]]+)\]\((https?://\S+)\)$")


def _safe_name_component(text: str, limit: int = 24) -> str:
    cleaned = _ILLEGAL_FILENAME_RE.sub(" ", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "文档")[:limit]


def _cleanup_old_documents(directory: Path, keep: int = _MAX_KEPT_FILES) -> None:
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
            LOGGER.warning("清理旧文档失败：%s", path)


def _render_blocks(doc: MiniDocx, content: str) -> None:
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") or stripped.startswith("## "):
            doc.heading(stripped.lstrip("#").strip() or "（小节）")
        elif stripped.startswith((">", "＞")):
            doc.meta(stripped.lstrip(">＞").strip())
        elif stripped.startswith(("- ", "* ", "－ ", "＊ ")):
            doc.bullet(stripped[2:].strip())
        else:
            link_match = _MARKDOWN_LINK_RE.match(stripped)
            if link_match:
                doc.link(link_match.group(1), link_match.group(2))
            elif _URL_RE.match(stripped):
                doc.link("链接", stripped)
            else:
                doc.paragraph(stripped)


def _build_docx(path: Path, title: str, content: str, stamp: datetime) -> None:
    doc = MiniDocx()
    doc.title(title)
    doc.meta(f"ATRI 整理 · 生成时间 {stamp.strftime('%Y-%m-%d %H:%M')} · 内容来自对话")
    _render_blocks(doc, content)
    doc.meta("本文档由 ATRI 依据对话内容整理生成。")
    doc.save(path)


def _build_markdown(path: Path, title: str, content: str, stamp: datetime) -> None:
    lines = [
        f"# {title}",
        "",
        f"> ATRI 整理 · 生成时间 {stamp.strftime('%Y-%m-%d %H:%M')} · 内容来自对话",
        "",
        content,
        "",
        "---",
        "本文档由 ATRI 依据对话内容整理生成。",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_text_document(
    *,
    title: str,
    content: str,
    output_dir: Path,
) -> dict[str, Any]:
    """把标题与轻量 Markdown 正文排版成 docx（失败降级 md），返回投递描述。"""
    title = str(title or "").strip()[:_MAX_TITLE_CHARS]
    content = str(content or "").strip()
    if not title or not content:
        return {"ok": False, "error": "标题或正文为空"}
    if len(content) > _MAX_CONTENT_CHARS:
        return {"ok": False, "error": f"正文超过 {_MAX_CONTENT_CHARS} 字，请精简后重新调用"}
    stamp = datetime.now()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"文档_{_safe_name_component(title)}_{stamp.strftime('%Y%m%d_%H%M%S')}"
    docx_path = output_dir / f"{stem}.docx"
    try:
        _build_docx(docx_path, title, content, stamp)
        format_name = "docx"
        result_path = docx_path
    except Exception:
        LOGGER.exception("通用文档 docx 生成失败，降级为 md")
        md_path = output_dir / f"{stem}.md"
        _build_markdown(md_path, title, content, stamp)
        format_name = "md"
        result_path = md_path
    _cleanup_old_documents(output_dir)
    LOGGER.info(
        "通用文档生成完成：format=%s title=%.60s chars=%d", format_name, title, len(content)
    )
    return {
        "ok": True,
        "format": format_name,
        "path": str(result_path),
        "name": result_path.name,
    }
