"""Who Is Who 的数据模型（移植自 astrbot_plugin_who_is_who）。

NormalizedMessage 是 OneBot 群消息的规范化表示：@ 关系、引用链和话题
归属都在这里，供上下文构建与规则提取使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class NormalizedMessage:
    message_id: str
    group_key: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: int
    mentions: list[str] = field(default_factory=list)
    mention_names: dict[str, str] = field(default_factory=dict)
    reply_to_message_id: str | None = None
    reply_sender_id: str | None = None
    reply_sender_name: str | None = None
    reply_text: str | None = None
    topic_id: str = "unassigned"
    role: str = "user"


@dataclass(slots=True)
class Authority:
    user_id: str
    priority: int
    permissions: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


@dataclass(slots=True)
class RuleCandidate:
    kind: str
    key: str
    content: str
    issuer_id: str
    target_id: str | None
    scope: str
    topic_id: str | None
    requires_consent: bool = False
    source_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
