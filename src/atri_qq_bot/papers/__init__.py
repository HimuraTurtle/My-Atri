"""学术论文检索（papers）：search_papers 检索与 compile_paper_digest 整理。"""

from __future__ import annotations

from typing import Any

from .digest import build_paper_digest
from .service import PapersService

__all__ = ["PapersService", "build_paper_digest", "get_papers_service"]

_SERVICE: PapersService | None = None


def get_papers_service(config: Any) -> PapersService | None:
    global _SERVICE
    if not bool(getattr(config, "papers_enabled", True)):
        return None
    if _SERVICE is None:
        _SERVICE = PapersService(config)
    return _SERVICE
