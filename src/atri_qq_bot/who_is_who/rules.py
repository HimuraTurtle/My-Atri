"""规则状态机（移植自 astrbot_plugin_who_is_who）。

候选 -> 状态（active/pending_response/pending_consent/conflicted/
rejected）；AI 回复接受后形成 commitment。冲突裁决遵守
插件管理员 > 本人偏好 > 授权用户 > 普通提议 的优先级。
"""

from __future__ import annotations

from .authority import PLUGIN_ADMIN_PRIORITY, SELF_PREFERENCE_PRIORITY
from .models import Authority, RuleCandidate
from .storage import Database


class RuleEngine:
    def __init__(self, db: Database, *, self_preference_first: bool = True,
                 allow_whitelist_override: bool = False,
                 newer_rule_wins: bool = True) -> None:
        self.db = db
        self.self_preference_first = self_preference_first
        self.allow_whitelist_override = allow_whitelist_override
        self.newer_rule_wins = newer_rule_wins

    def create_request(
        self,
        group_key: str,
        candidate: RuleCandidate,
        authority: Authority,
    ) -> tuple[int, str]:
        status = "pending_response"
        reason = "等待 AI 回复确认是否形成承诺"
        effective_priority = authority.priority
        is_plugin_admin = "plugin_admin" in authority.sources

        if (
            candidate.kind == "preference"
            and candidate.target_id == candidate.issuer_id
            and not (is_plugin_admin and candidate.requires_consent)
        ):
            effective_priority = (
                SELF_PREFERENCE_PRIORITY
                if self.self_preference_first
                else authority.priority
            )
            existing = self.db.get_active_conflict(
                group_key,
                candidate.key,
                candidate.scope,
                candidate.target_id,
                candidate.topic_id,
            )
            if existing and existing["priority"] > effective_priority:
                status = "conflicted"
                reason = "与插件管理员设置的更高等级偏好冲突，不能覆盖"
            else:
                status = "active"
                reason = "用户本人设置自己的偏好"
                self.db.supersede_states(
                    group_key,
                    candidate.key,
                    candidate.scope,
                    candidate.target_id,
                    effective_priority,
                    self.newer_rule_wins,
                )
        elif candidate.requires_consent:
            if is_plugin_admin:
                effective_priority = PLUGIN_ADMIN_PRIORITY
                status = "active"
                reason = "由插件管理员直接设置，拥有本插件最高等级"
                self.db.supersede_states(
                    group_key,
                    candidate.key,
                    candidate.scope,
                    candidate.target_id,
                    effective_priority,
                    True,
                )
            else:
                can_override = (
                    authority.can("override_commitment")
                    and self.allow_whitelist_override
                    and "explicit" in authority.sources
                )
                if self.self_preference_first and not can_override:
                    status = "pending_consent"
                    reason = "涉及第三方个人偏好，等待目标用户确认"
                elif authority.can("create_persistent_rule"):
                    status = "pending_response"
                    reason = "配置允许有权用户覆盖第三方个人偏好，等待 AI 回复"
                else:
                    status = "rejected"
                    reason = "提出者没有设置第三方长期偏好的权限"
        elif candidate.scope == "group" and not authority.can("create_group_rule"):
            status = "rejected"
            reason = "提出者没有创建群级规则的权限"
        elif candidate.scope == "user" and not authority.can(
            "create_persistent_rule"
        ):
            if candidate.target_id != candidate.issuer_id:
                status = "rejected"
                reason = "提出者没有创建长期规则的权限"

        if status == "pending_response":
            existing = self.db.get_active_conflict(
                group_key,
                candidate.key,
                candidate.scope,
                candidate.target_id,
                candidate.topic_id,
            )
            if existing and (
                existing["priority"] > authority.priority
                or (
                    existing["priority"] == authority.priority
                    and not self.newer_rule_wins
                )
            ):
                status = "conflicted"
                reason = (
                    f"与优先级 {existing['priority']} 的现有有效状态冲突，"
                    "当前请求不能覆盖"
                )

        state_id = self.db.add_state(
            group_key=group_key,
            kind=candidate.kind,
            state_key=candidate.key,
            content=candidate.content,
            issuer_id=candidate.issuer_id,
            target_id=candidate.target_id,
            scope=candidate.scope,
            topic_id=candidate.topic_id,
            priority=effective_priority,
            status=status,
            reason=reason,
            source_message_id=candidate.source_message_id,
            metadata=candidate.metadata,
        )
        if (
            status == "active"
            and candidate.key == "preferred_name"
            and candidate.target_id
        ):
            preferred_name = str(candidate.metadata.get("value", ""))
            if preferred_name:
                self.db.set_preferred_name(group_key, candidate.target_id, preferred_name)
        return state_id, status

    def resolve_ai_response(self, request_id: int, accepted: bool, ai_message_id: str) -> None:
        request = self.db.get_state(request_id)
        if not request or request["status"] in {"rejected", "cancelled", "expired"}:
            return
        if not accepted:
            self.db.update_state_status(request_id, "rejected", "AI 未接受该请求")
            return

        status = request["status"]
        if status == "conflicted":
            return
        if status == "pending_response":
            self.db.supersede_states(
                request["group_key"],
                request["state_key"],
                request["scope"],
                request["target_id"],
                request["priority"],
                self.newer_rule_wins,
            )
            status = "active"
            self.db.update_state_status(request_id, status, "AI 已接受该请求")

        self.db.add_state(
            group_key=request["group_key"],
            kind="commitment",
            state_key=request["state_key"],
            content=f"AI 已答应：{request['content']}",
            issuer_id=request["issuer_id"],
            target_id=request["target_id"],
            scope=request["scope"],
            topic_id=request["topic_id"],
            priority=request["priority"],
            status=status,
            reason="由用户请求与 AI 接受回复共同形成",
            source_message_id=ai_message_id,
            parent_id=request_id,
            metadata={"request_source": request["source_message_id"]},
        )

    def reject_pending_for_target(
        self, group_key: str, target_id: str, issuer_ids: list[str] | None = None
    ) -> int:
        return self.db.reject_pending_for_target(group_key, target_id, issuer_ids)

    def approve_pending_for_target(
        self, group_key: str, target_id: str, issuer_ids: list[str] | None = None
    ) -> int:
        return self.db.approve_pending_for_target(
            group_key, target_id, issuer_ids, SELF_PREFERENCE_PRIORITY
        )
