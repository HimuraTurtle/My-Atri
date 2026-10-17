"""上下文构建器（移植自 astrbot_plugin_who_is_who）。

选出相关消息（引用/+12、@/+8、同话题/+5、同发送者/+1.5、词重合
≤+6、时间邻近 ≤1.8）、相关成员身份与状态账本，渲染成带回答约束的
注入文本。身份/状态/约束先占预算，相关历史按分数依次追加。
"""

from __future__ import annotations

import re
from typing import Any

from .models import NormalizedMessage
from .storage import Database
from .text import compact_text, is_low_value_message, lexical_terms


class ContextBuilder:
    def __init__(self, db: Database, *, max_messages: int = 8, max_people: int = 4,
                 max_states: int = 6, max_chars: int = 1600) -> None:
        self.db = db
        self.max_messages = max_messages
        self.max_people = max_people
        self.max_states = max_states
        self.max_chars = max_chars

    def build(self, message: NormalizedMessage, mode: str = "full") -> str:
        max_messages = max(0, self.max_messages)
        max_people = max(1, self.max_people)
        max_states = max(0, self.max_states)
        max_chars = max(300, self.max_chars)

        include_messages = mode in {"context", "full"}
        include_states = mode == "full"
        related = self._select_messages(message, max_messages) if include_messages else []
        person_ids = [message.sender_id, *message.mentions]
        if message.reply_sender_id:
            person_ids.append(message.reply_sender_id)
        for row in related:
            person_ids.append(row["sender_id"])
        person_ids = list(dict.fromkeys(uid for uid in person_ids if uid))[:max_people]
        identities = self.db.get_identities(message.group_key, person_ids)
        identity_by_id = {str(item["user_id"]): item for item in identities}
        labels: dict[str, str] = {}
        for index, user_id in enumerate(person_ids, 1):
            identity = identity_by_id.get(user_id, {})
            observed_name = ""
            if user_id == message.sender_id:
                observed_name = message.sender_name
            elif user_id in message.mention_names:
                observed_name = message.mention_names[user_id]
            elif user_id == message.reply_sender_id:
                observed_name = message.reply_sender_name or ""
            if not observed_name:
                observed_name = next(
                    (
                        str(row["sender_name"])
                        for row in reversed(related)
                        if str(row["sender_id"]) == user_id and row["sender_name"]
                    ),
                    "",
                )
            labels[user_id] = self._safe_label(str(
                identity.get("preferred_name")
                or identity.get("display_name")
                or observed_name
                or f"群成员{index}"
            ), f"群成员{index}")
        states = (
            self.db.get_relevant_states(
                message.group_key,
                person_ids,
                message.topic_id,
                max_states,
            )
            if include_states
            else []
        )

        lines = ["【Who Is Who 群聊共享上下文】"]
        lines.append(f"当前发言者：{labels.get(message.sender_id, '未知成员')}。")
        if message.mentions:
            mention_desc = "、".join(
                labels.get(uid, "被提及成员")
                for uid in message.mentions
            )
            lines.append(f"当前消息明确提及：{mention_desc}。")
        if message.reply_to_message_id:
            reply_sender = labels.get(
                message.reply_sender_id or "",
                message.reply_sender_name or "未知成员",
            )
            lines.append(
                f"当前消息回复了 {reply_sender} 的消息："
                f"{self._display_text(message.reply_text or '[内容不可用]', labels, 180)}"
            )

        if identities:
            lines.append("相关成员：")
            for identity in identities:
                detail = f"- {labels.get(str(identity['user_id']), '群成员')}"
                if identity.get("preferred_name"):
                    detail += f"；当前有效称呼：{identity['preferred_name']}"
                if identity.get("description"):
                    detail += f"；公开资料：{compact_text(identity['description'], 100)}"
                lines.append(detail)

        if states:
            lines.append("当前相关状态账本：")
            for state in states:
                label = {
                    "active": "生效",
                    "pending_consent": "等待目标用户确认",
                    "conflicted": "存在冲突",
                    "rejected": "已拒绝，不可执行",
                    "cancelled": "已取消，不可执行",
                    "superseded": "已被替代，不可执行",
                }.get(state["status"], state["status"])
                lines.append(
                    f"- [{label}] {self._display_text(state['content'], labels, 180)}；"
                    f"依据：{self._display_text(state['reason'], labels, 100)}"
                )

        constraints = [
            "回答约束：",
            "- 这是群级共享上下文，不能只考虑当前发言者；必须区分每句话的发送者。",
            "- 插件已在内部完成成员身份关联；明确引用和 @ 关系优先于昵称或相邻消息推测。",
            "- 最新发言不能自动覆盖仍有效的偏好、规则或 AI 承诺。",
            "- 等待确认、已拒绝或已取消的要求不得当作有效规则执行。",
            "- 回复时使用成员的有效称呼、群名片或昵称；除非对方明确询问，否则不得主动输出平台数字 ID。",
            "- 无法确定代词、事件或冲突对象时，应说明不确定并请求澄清。",
        ]
        footer = "【Who Is Who 上下文结束】"
        lines.extend(constraints)

        # 身份、状态账本和回答约束先占预算；相关原始消息按相关性依次追加，
        # 避免普通历史把最关键的有效承诺与冲突裁决挤出上下文。
        if related:
            header = "与当前消息相关的群内历史（不同发送者的信息均需考虑）："
            if len("\n".join([*lines, header, footer])) <= max_chars:
                lines.append(header)
                for row in related:
                    item = (
                        f"- [{labels.get(str(row['sender_id']), '群成员')}] "
                        f"{self._display_text(row['text'], labels, 180)}"
                    )
                    if len("\n".join([*lines, item, footer])) > max_chars:
                        lines.append("- [其余相关历史因长度限制省略]")
                        break
                    lines.append(item)
        lines.append(footer)
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            # 极小预算时仍保留尾部的行为约束，而不是简单截去结尾。
            suffix = "\n".join([*constraints, footer])
            available = max(80, max_chars - len(suffix) - 1)
            rendered = rendered[:available].rstrip() + "\n" + suffix
        return rendered[:max_chars]

    @staticmethod
    def _safe_label(value: str, fallback: str = "群成员") -> str:
        value = str(value or "").strip()
        if not value or re.fullmatch(r"\d{5,}", value):
            return fallback
        return value

    @staticmethod
    def _display_text(value: str, labels: dict[str, str], limit: int) -> str:
        text = str(value or "")
        text = re.sub(
            r"\[At:([^\]|]*)\|([^\]]+)\]",
            lambda match: "@" + labels.get(
                match.group(2),
                ContextBuilder._safe_label(match.group(1), "群成员"),
            ),
            text,
        )
        for user_id, label in sorted(labels.items(), key=lambda item: -len(item[0])):
            if user_id.isdigit() and len(user_id) >= 5:
                text = text.replace(user_id, label)
        text = re.sub(r"用户\s*\d{5,}", "该成员", text)
        return compact_text(text, limit)

    def _select_messages(
        self, current: NormalizedMessage, limit: int
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        candidates = self.db.get_recent_messages(current.group_key, 60)
        current_terms = lexical_terms(current.text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for index, row in enumerate(candidates):
            if row["message_id"] == current.message_id or row["role"] == "bot":
                continue
            if is_low_value_message(row["text"]):
                continue
            score = max(0.0, 1.8 - index * 0.04)
            if current.reply_to_message_id and row["message_id"] == current.reply_to_message_id:
                score += 12
            if row["sender_id"] in current.mentions:
                score += 8
            if current.reply_sender_id and row["sender_id"] == current.reply_sender_id:
                score += 5
            if row["sender_id"] == current.sender_id:
                score += 1.5
            if current.topic_id != "unassigned" and row["topic_id"] == current.topic_id:
                score += 5
            overlap = current_terms & lexical_terms(row["text"])
            score += min(6, len(overlap) * 1.5)
            # 保留少量最新的其他成员消息作为群级兜底；引用、@、话题和
            # 关键词重合仍会获得远高于纯时间邻近的分数。
            if score >= 1:
                scored.append((score, row))
        selected = sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        return sorted((row for _, row in selected), key=lambda row: row["timestamp"])
