"""MiniMax 绘图模块对外入口。"""

from __future__ import annotations

from typing import Any

from .service import (
    MinimaxDrawError,
    MinimaxDrawService,
    STYLE_PRESETS,
    SUPPORTED_ASPECT_RATIOS,
)

_SERVICE: MinimaxDrawService | None = None


def get_minimax_draw_service(config: Any) -> MinimaxDrawService | None:
    """按 BotConfig 获取全局单例；未启用时返回 None。"""
    global _SERVICE
    if not bool(getattr(config, "minimax_draw_enabled", False)):
        return None
    if _SERVICE is None:
        _SERVICE = MinimaxDrawService(config)
    return _SERVICE


__all__ = [
    "MinimaxDrawService",
    "MinimaxDrawError",
    "STYLE_PRESETS",
    "SUPPORTED_ASPECT_RATIOS",
    "get_minimax_draw_service",
]
