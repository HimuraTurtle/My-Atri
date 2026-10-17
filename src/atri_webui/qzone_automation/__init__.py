from __future__ import annotations

from .rules import (
    DEFAULT_SETTINGS,
    DEFAULT_TOPIC_ROTATION,
    LEGACY_TOPIC_ROTATION,
    MAX_RECENT_COMMENT_FEEDS,
    content_quality_issues,
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
    "DEFAULT_SETTINGS",
    "LEGACY_TOPIC_ROTATION",
    "content_quality_issues",
    "next_window_time",
    "schedule_times",
    "topic_prompt",
    "parse_keywords",
    "sanitize_qzone_fact_context",
    "topic_for_schedule",
]
