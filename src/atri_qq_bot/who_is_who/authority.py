"""权限引擎（移植自 astrbot_plugin_who_is_who，配置适配 ATRi 扁平参数）。

优先级层级：插件管理员（200 万）> 本人偏好（100 万）> 显式白名单 >
受限用户（0）> 默认（10）。插件管理员 = ATRi owner ∪ wiw_admin_ids，
不认群主/群管理员头衔，与原插件哲学一致。
"""

from __future__ import annotations

from typing import Any

from .models import Authority

ALL_MANAGEMENT_PERMISSIONS = {
    "manage_plugin",
    "manage_authority",
    "manage_memory",
    "manage_profiles",
    "create_group_rule",
    "create_persistent_rule",
    "override_commitment",
    "view_debug_context",
}

SELF_PREFERENCE_PRIORITY = 1_000_000
PLUGIN_ADMIN_PRIORITY = 2_000_000


class AuthorityEngine:
    def __init__(
        self,
        *,
        admins: set[str] | None = None,
        restricted: set[str] | None = None,
        explicit_users: list[dict[str, Any]] | None = None,
        default_priority: int = 10,
        enabled: bool = True,
    ) -> None:
        self.admins = {str(item) for item in (admins or set())}
        self.restricted = {str(item) for item in (restricted or set())}
        self.explicit_users = list(explicit_users or [])
        self.default_priority = int(default_priority)
        self.enabled = bool(enabled)

    def resolve(self, user_id: str, group_id: str) -> Authority:
        user_id = str(user_id or "")
        group_id = str(group_id or "")
        if user_id in self.admins:
            return Authority(user_id, PLUGIN_ADMIN_PRIORITY, {"*"}, ["plugin_admin"])
        if not self.enabled:
            return Authority(user_id, self.default_priority, set(), ["authority_disabled"])
        if user_id in self.restricted:
            return Authority(user_id, 0, set(), ["restricted"])
        explicit = self._find_explicit(user_id, group_id)
        if explicit is not None:
            return explicit
        return Authority(user_id, self.default_priority, set(), ["default"])

    def _find_explicit(self, user_id: str, group_id: str) -> Authority | None:
        for entry in self.explicit_users:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("user_id", "")) != user_id:
                continue
            entry_group = str(entry.get("group_id", "*") or "*")
            if entry_group not in {"*", group_id}:
                continue
            return Authority(
                user_id,
                int(entry.get("priority", 50)),
                set(entry.get("permissions", [])),
                ["explicit"],
            )
        return None
