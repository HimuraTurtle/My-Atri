"""事实核查（是真的吗）：自然语言触发的 fact_check LLM 工具。"""

from __future__ import annotations

from typing import Any

from .service import IsItTrueService

_SERVICE: IsItTrueService | None = None


def get_isittrue_service(config: Any) -> IsItTrueService | None:
    global _SERVICE
    if not bool(getattr(config, "isittrue_enabled", False)):
        return None
    if _SERVICE is None:
        _SERVICE = IsItTrueService(config)
    return _SERVICE
