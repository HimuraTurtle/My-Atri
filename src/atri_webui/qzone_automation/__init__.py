from __future__ import annotations

from .devlog import DevLogService, classify_commit, is_sensitive_subject
from .rules import (
    DEFAULT_SETTINGS,
    DEFAULT_TOPIC_ROTATION,
    DEVLOG_MAX_COMMITS_PER_POST,
    LEGACY_TOPIC_ROTATION,
    MAX_RECENT_COMMENT_FEEDS,
    content_quality_issues,
    devlog_material_prompt,
    next_window_time,
    parse_keywords,
    sanitize_qzone_fact_context,
    schedule_times,
    topic_for_schedule,
    topic_prompt,
)
from .automation import QZoneAutomationMixin
from .store import QZoneStoreMixin


class QZoneAutomationStore(QZoneStoreMixin):
    """Stable SQLite interface for QZone automation state."""


class QZoneAutomation(QZoneAutomationMixin):
    """Stable automation interface assembled from store and scheduling modules."""


__all__ = [
    "QZoneAutomationStore",
    "QZoneAutomation",
    "DevLogService",
    "DEFAULT_SETTINGS",
    "DEVLOG_MAX_COMMITS_PER_POST",
    "LEGACY_TOPIC_ROTATION",
    "classify_commit",
    "content_quality_issues",
    "devlog_material_prompt",
    "is_sensitive_subject",
    "next_window_time",
    "schedule_times",
    "topic_prompt",
    "parse_keywords",
    "sanitize_qzone_fact_context",
    "topic_for_schedule",
]
