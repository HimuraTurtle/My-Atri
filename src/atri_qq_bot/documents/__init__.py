"""通用文档输出（documents）：send_document 把整理内容排版成 Word 发送。"""

from __future__ import annotations

from typing import Any

from .service import DocumentsService
from .writer import build_text_document

__all__ = ["DocumentsService", "build_text_document", "get_documents_service"]

_SERVICE: DocumentsService | None = None


def get_documents_service(config: Any) -> DocumentsService | None:
    global _SERVICE
    if not bool(getattr(config, "documents_enabled", True)):
        return None
    if _SERVICE is None:
        _SERVICE = DocumentsService()
    return _SERVICE
