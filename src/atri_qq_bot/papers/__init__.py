"""学术论文检索（papers）：search_papers LLM 工具，OpenAlex 数据源。"""

from __future__ import annotations

from typing import Any

from .service import PapersService

_SERVICE: PapersService | None = None


def get_papers_service(config: Any) -> PapersService | None:
    global _SERVICE
    if not bool(getattr(config, "papers_enabled", True)):
        return None
    if _SERVICE is None:
        _SERVICE = PapersService(config)
    return _SERVICE
