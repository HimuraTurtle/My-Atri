"""LivingMemory 记忆原子：带 TTL、衰减曲线和强化机制的记忆单元。

设计移植自 astrbot_plugin_livingmemory（AGPLv3，作者 lxfight）的
memory_atom 模型：每条记忆按类型有基准 TTL 和衰减曲线，重要性越高、
被强化次数越多，存活越久；检索时按距上次访问的时间算时间分。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

EPISODIC = "episodic"
FACTUAL = "factual"
RELATIONAL = "relational"
PREFERENCE = "preference"
PLANNED = "planned"
UNKNOWN = "unknown"

LINEAR = "linear"
EXPONENTIAL = "exponential"
STEP = "step"

ACTIVE = "active"
DORMANT = "dormant"
FORGOTTEN = "forgotten"

# 类型 -> (基准 TTL 天数, 衰减曲线)。事实类活得久，情节类一周自然淡忘。
_TTL_CONFIG: dict[str, tuple[float, str]] = {
    EPISODIC: (7.0, EXPONENTIAL),
    PLANNED: (2.0, STEP),
    FACTUAL: (180.0, EXPONENTIAL),
    RELATIONAL: (90.0, LINEAR),
    PREFERENCE: (60.0, EXPONENTIAL),
    UNKNOWN: (30.0, EXPONENTIAL),
}


def compute_decay_score(decay_type: str, ttl_days: float, days_since: float) -> float:
    """返回某条记忆在给定 TTL 与距上次访问天数下的衰减乘数 (0~1)。"""
    ttl = max(1.0, float(ttl_days))
    age = max(0.0, float(days_since))
    if decay_type == LINEAR:
        return max(0.0, 1.0 - age / ttl)
    if decay_type == STEP:
        return 1.0 if age <= ttl else 0.05
    half_life = ttl / 2.0
    return math.exp(-math.log(2) * age / max(0.5, half_life))


def compute_ttl(
    atom_type: str,
    importance: float = 0.5,
    reinforcement_count: int = 0,
) -> tuple[float, str]:
    """按类型/重要性/强化次数计算 TTL（天）与衰减曲线。"""
    base_ttl, decay_type = _TTL_CONFIG.get(str(atom_type), _TTL_CONFIG[UNKNOWN])
    importance_factor = 0.5 + max(0.0, min(1.0, float(importance)))
    reinforcement_factor = 1.0 + min(0.5, int(reinforcement_count) * 0.1)
    ttl = float(base_ttl) * importance_factor * reinforcement_factor
    return max(1.0, ttl), decay_type


@dataclass
class MemoryAtom:
    """一条可检索的长期记忆。"""

    session_id: str
    content: str
    atom_type: str = EPISODIC
    topics: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    importance: float = 0.5
    sentiment: str = "neutral"
    ttl_days: float = 7.0
    decay_type: str = EXPONENTIAL
    status: str = ACTIVE
    reinforcement_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    last_reinforced_at: float | None = None
    expires_at: float = 0.0
    source_start: float = 0.0
    source_end: float = 0.0
    source_label: str = ""
    memory_id: int = 0

    def __post_init__(self) -> None:
        if self.expires_at <= 0.0:
            self.ttl_days, self.decay_type = compute_ttl(
                self.atom_type, self.importance, self.reinforcement_count
            )
            self.expires_at = self.created_at + self.ttl_days * 86400.0
        if self.last_accessed_at <= 0.0:
            self.last_accessed_at = self.created_at

    def temporal_score(self, reference_time: float | None = None) -> float:
        now = reference_time if reference_time is not None else time.time()
        days_since = max(0.0, (now - self.last_accessed_at) / 86400.0)
        return compute_decay_score(self.decay_type, self.ttl_days, days_since)

    def reinforce(self, now: float) -> None:
        """相似内容再次出现：强化计数+1 并延长 TTL。"""
        self.reinforcement_count += 1
        self.last_reinforced_at = now
        self.ttl_days, self.decay_type = compute_ttl(
            self.atom_type, self.importance, self.reinforcement_count
        )
        self.expires_at = now + self.ttl_days * 86400.0
        self.last_accessed_at = now
        self.status = ACTIVE

    def touch(self, now: float) -> None:
        self.last_accessed_at = now

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MemoryAtom:
        return cls(
            session_id=str(row.get("session_id") or ""),
            content=str(row.get("content") or ""),
            atom_type=str(row.get("atom_type") or EPISODIC),
            topics=list(row.get("topics") or []),
            key_facts=list(row.get("key_facts") or []),
            importance=float(row.get("importance") or 0.5),
            sentiment=str(row.get("sentiment") or "neutral"),
            ttl_days=float(row.get("ttl_days") or 7.0),
            decay_type=str(row.get("decay_type") or EXPONENTIAL),
            status=str(row.get("status") or ACTIVE),
            reinforcement_count=int(row.get("reinforcement_count") or 0),
            created_at=float(row.get("created_at") or 0.0),
            last_accessed_at=float(row.get("last_accessed_at") or 0.0),
            last_reinforced_at=(
                float(row["last_reinforced_at"])
                if row.get("last_reinforced_at") is not None
                else None
            ),
            expires_at=float(row.get("expires_at") or 0.0),
            source_start=float(row.get("source_start") or 0.0),
            source_end=float(row.get("source_end") or 0.0),
            source_label=str(row.get("source_label") or ""),
            memory_id=int(row.get("memory_id") or 0),
        )
