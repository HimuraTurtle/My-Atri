"""舞萌 DX 查分器（maimai）：search_maimai_best50 LLM 工具，落雪数据源。"""

from __future__ import annotations

from typing import Any

from .service import MaimaiService

_SERVICE: MaimaiService | None = None


def get_maimai_service(config: Any) -> MaimaiService | None:
    global _SERVICE
    if not bool(getattr(config, "maimai_enabled", True)):
        return None
    if not str(getattr(config, "maimai_lxns_dev_token", "") or "").strip():
        return None
    if _SERVICE is None:
        _SERVICE = MaimaiService(config)
    return _SERVICE
