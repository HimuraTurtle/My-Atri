"""QQ 空间模块：NapCat 会话复用式说说/点赞/评论与开发日志闭环。

与旧 atri_webui.qzone_automation 的本质差异：不再维护独立登录桥与常驻
轮询，Cookie 通过 OneBot ``get_cookies`` 按需借取，流量只在使用时产生。
"""

from __future__ import annotations

from typing import Any

from .api import QzoneAPI
from .devlog import DevLog, DevLogUnavailable, classify_commit, is_sensitive_subject
from .rules import build_comment_prompt, build_post_prompt
from .service import CookieProvider, QzoneService, TextGenerator
from .session import QzoneContext, QzoneSession
from .store import QzoneStore

_SERVICE: QzoneService | None = None


def get_qzone_service(config: Any) -> QzoneService | None:
    """进程级单例；qzone_enabled 关闭时返回 None，调用方按未启用处理。"""
    global _SERVICE
    if not bool(getattr(config, "qzone_enabled", False)):
        return None
    if _SERVICE is None:
        _SERVICE = QzoneService(config)
    return _SERVICE


def reset_qzone_service() -> None:
    """测试隔离用：丢弃单例（不删除 SQLite 数据）。"""
    global _SERVICE
    _SERVICE = None


__all__ = [
    "QzoneAPI",
    "QzoneContext",
    "QzoneSession",
    "QzoneStore",
    "QzoneService",
    "DevLog",
    "DevLogUnavailable",
    "CookieProvider",
    "TextGenerator",
    "classify_commit",
    "is_sensitive_subject",
    "build_comment_prompt",
    "build_post_prompt",
    "get_qzone_service",
    "reset_qzone_service",
]
