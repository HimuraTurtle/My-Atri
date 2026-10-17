"""Who Is Who（移植自 astrbot_plugin_who_is_who）。

群聊共享上下文 + 成员称呼学习 + 规则与 AI 承诺一致性。全部本地规则
处理（正则 + SQLite），不增加额外 LLM 调用。

链路：
- record_group_message：每条群消息归一化入库（@/引用/话题归属），
  顺带做称呼学习与用户同意/拒绝检测；
- prepare_turn：回复门控通过后构建共享上下文（相关消息 + 身份 +
  状态账本），并检测长期请求形成 pending_response；
- finish_turn：AI 回复落地后记录 bot 消息，并把 pending 请求按
  AI 是否接受转成承诺或拒绝。

与原插件的取舍：
- 权限：插件管理员 = ATRi owner_qqs ∪ wiw_admin_ids；/wiw priority
  指令砍掉（运行时改不了 .env），显式白名单走 WIW_EXPLICIT_USERS_JSON；
- 引用链：不做 get_msg 网络调用，从本库已记录消息反查 reply 发送者；
- 存储路径 data/who_is_who/who_is_who.db（WAL + busy_timeout）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bilibili.parsing import strip_address_prefix
from .authority import AuthorityEngine, PLUGIN_ADMIN_PRIORITY, SELF_PREFERENCE_PRIORITY
from .context import ContextBuilder
from .models import Authority, NormalizedMessage, RuleCandidate
from .rules import RuleEngine
from .storage import Database
from .text import (
    ai_accepted,
    extract_rule_candidate,
    is_consent,
    is_refusal,
    is_low_value_message,
    lexical_terms,
    topic_fingerprint,
)

LOGGER = logging.getLogger("atri.who_is_who")

VALID_MODES = {"record_only", "identity_only", "context", "full"}


@dataclass(frozen=True)
class PreparedTurn:
    context_text: str
    request_id: int | None
    topic_id: str
    group_key: str


class WhoIsWhoService:
    def __init__(self, config: Any) -> None:
        self.enabled = bool(getattr(config, "wiw_enabled", True))
        mode = str(getattr(config, "wiw_mode", "full") or "full").strip().lower()
        self.mode = mode if mode in VALID_MODES else "full"
        self.learn_nicknames = bool(getattr(config, "wiw_learn_nicknames", True))
        self.detect_commitments = bool(getattr(config, "wiw_detect_commitments", True))
        self.topic_active_minutes = max(1, int(getattr(config, "wiw_topic_active_minutes", 30)))
        self.max_messages_per_group = max(20, int(getattr(config, "wiw_max_messages_per_group", 500)))
        self.retention_days = max(1, int(getattr(config, "wiw_retention_days", 30)))
        self.bot_qq = str(getattr(config, "bot_qq", "") or "")

        self.enabled_groups = {
            str(item) for item in (getattr(config, "wiw_enabled_groups", ()) or ())
        }
        self.blocked_groups = {
            str(item) for item in (getattr(config, "wiw_blocked_groups", ()) or ())
        }

        admins = {str(item) for item in (getattr(config, "owner_qqs", ()) or ())}
        admins |= {str(item) for item in (getattr(config, "wiw_admin_ids", ()) or ())}
        restricted = {str(item) for item in (getattr(config, "wiw_restricted_ids", ()) or ())}

        default_db_path = Path("data") / "who_is_who" / "who_is_who.db"
        db_path = Path(getattr(config, "wiw_db_path", None) or default_db_path)
        self.db = Database(db_path)
        self.authority = AuthorityEngine(
            admins=admins,
            restricted=restricted,
            explicit_users=self._load_explicit_users(config),
            default_priority=int(getattr(config, "wiw_default_priority", 10)),
            enabled=bool(getattr(config, "wiw_authority_enabled", True)),
        )
        self.rules = RuleEngine(
            self.db,
            self_preference_first=bool(getattr(config, "wiw_self_preference_first", True)),
            allow_whitelist_override=bool(getattr(config, "wiw_allow_whitelist_override", False)),
            newer_rule_wins=bool(getattr(config, "wiw_newer_rule_wins", True)),
        )
        self.context_builder = ContextBuilder(
            self.db,
            max_messages=int(getattr(config, "wiw_max_related_messages", 8)),
            max_people=int(getattr(config, "wiw_max_related_people", 4)),
            max_states=int(getattr(config, "wiw_max_related_states", 6)),
            max_chars=int(getattr(config, "wiw_max_injection_chars", 1600)),
        )

        try:
            self.db.cleanup(self.retention_days)
        except Exception:
            LOGGER.exception("Who Is Who 启动清理失败")

    @staticmethod
    def _load_explicit_users(config: Any) -> list[dict[str, Any]]:
        raw = str(getattr(config, "wiw_explicit_users_json", "") or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            LOGGER.warning("WIW_EXPLICIT_USERS_JSON 解析失败，忽略白名单：%s", exc)
            return []
        if not isinstance(parsed, list):
            LOGGER.warning("WIW_EXPLICIT_USERS_JSON 必须是数组，忽略")
            return []
        return [item for item in parsed if isinstance(item, dict)]

    # ── 启用判定 ──

    def enabled_for_event(self, event: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        if str(event.get("message_type") or "") != "group":
            return False
        group_id = str(event.get("group_id") or "")
        if not group_id:
            return False
        if group_id in self.blocked_groups:
            return False
        return not self.enabled_groups or group_id in self.enabled_groups

    @staticmethod
    def _group_key(event: dict[str, Any]) -> str:
        return f"qq:{event.get('group_id')}"

    # ── 消息归一化 ──

    def normalize(self, event: dict[str, Any]) -> NormalizedMessage:
        group_key = self._group_key(event)
        segments = event.get("message") or []
        if not isinstance(segments, list):
            segments = []

        reply_id = ""
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").lower() == "reply":
                data = segment.get("data") or {}
                value = data.get("id") or data.get("message_id")
                reply_id = str(value) if value not in (None, "") else ""
                break

        # 引用链从本库反查：bot 常驻群里，被引用消息大多已记录在库，
        # 避免 normalize 路径上发起 get_msg 网络调用。
        reply_sender_id = reply_sender_name = reply_text = None
        if reply_id:
            replied = self.db.get_message(group_key, reply_id)
            if replied:
                reply_sender_id = str(replied["sender_id"]) or None
                reply_sender_name = str(replied["sender_name"]) or None
                reply_text = str(replied["text"]) or None

        sender = event.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        sender_name = str(
            sender.get("card") or sender.get("nickname") or event.get("user_id") or ""
        )

        text_parts: list[str] = []
        mentions: list[str] = []
        mention_names: dict[str, str] = {}
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            if segment_type == "text":
                text_parts.append(str(data.get("text", "")))
            elif segment_type == "at":
                qq = str(data.get("qq", "") or "")
                name = str(data.get("name") or data.get("card") or qq)
                text_parts.append(f"[At:{name}|{qq}]")
                if qq and qq not in {"all", self.bot_qq} and qq not in mentions:
                    mentions.append(qq)
                    mention_names[qq] = name
            elif segment_type == "reply":
                if reply_sender_id:
                    text_parts.append(
                        f"[引用:{reply_sender_name or reply_sender_id}:{reply_text or '内容不可用'}]"
                    )
                else:
                    text_parts.append("[引用]")
            elif segment_type == "image":
                text_parts.append("[图片]")
            elif segment_type == "record":
                text_parts.append("[语音]")
            elif segment_type == "video":
                text_parts.append("[视频]")
            elif segment_type == "file":
                text_parts.append("[文件]")
            elif segment_type in {"mface", "marketface"}:
                text_parts.append("[动画表情]")
            elif segment_type in {"json", "xml", "share"}:
                text_parts.append("[分享]")
            else:
                text_parts.append(f"[{segment_type}]")

        message = NormalizedMessage(
            message_id=str(event.get("message_id") or uuid.uuid4().hex),
            group_key=group_key,
            sender_id=str(event.get("user_id") or "unknown"),
            sender_name=sender_name,
            text=" ".join(part for part in text_parts if part).strip(),
            timestamp=int(event.get("time") or time.time()),
            mentions=mentions,
            mention_names=mention_names,
            reply_to_message_id=reply_id or None,
            reply_sender_id=reply_sender_id,
            reply_sender_name=reply_sender_name,
            reply_text=reply_text,
        )
        # 补充“直接写昵称、没有 @”的对象识别：只匹配本群已观察到的
        # 稳定昵称/偏好称呼，且至少两个字符，降低普通词误命中。
        try:
            for identity in self.db.get_group_identities(message.group_key):
                user_id = str(identity["user_id"])
                if user_id == message.sender_id or user_id in message.mentions:
                    continue
                names = [identity.get("preferred_name", ""), identity.get("display_name", "")]
                try:
                    names.extend(json.loads(identity.get("aliases_json") or "[]"))
                except (TypeError, ValueError):
                    pass
                matched = next(
                    (
                        str(name)
                        for name in names
                        if name and len(str(name).strip()) >= 2 and str(name).strip() in message.text
                    ),
                    None,
                )
                if matched:
                    message.mentions.append(user_id)
                    message.mention_names[user_id] = matched
        except Exception:
            LOGGER.exception("Who Is Who 身份反查失败")
        message.topic_id = self.route_topic(message)
        return message

    def route_topic(self, message: NormalizedMessage) -> str:
        if is_low_value_message(message.text):
            return "unassigned"
        if message.reply_to_message_id:
            replied = self.db.get_message(message.group_key, message.reply_to_message_id)
            if replied and replied["topic_id"] != "unassigned":
                return replied["topic_id"]

        terms = lexical_terms(message.text)
        if not terms:
            return "unassigned"
        after = message.timestamp - self.topic_active_minutes * 60
        topics = self.db.get_active_topics(message.group_key, after)
        best = None
        best_score = 0.0
        for topic in topics:
            overlap = len(terms & topic["terms"])
            union = len(terms | topic["terms"]) or 1
            score = overlap / union
            if overlap >= 1 and score > best_score:
                best = topic
                best_score = score
        if best and best_score >= 0.08:
            topic_id = best["topic_id"]
            terms = set(best["terms"]) | terms
        else:
            topic_id = f"topic_{topic_fingerprint({message.group_key, *terms})}"
        self.db.upsert_topic(topic_id, message.group_key, set(list(terms)[:40]), message.timestamp)
        return topic_id

    def save(self, message: NormalizedMessage) -> None:
        self.db.save_message(message, self.max_messages_per_group)

    # ── 事件接入 ──

    def record_group_message(self, event: dict[str, Any], plain_text: str) -> None:
        """每条群消息：归一化入库 + 称呼学习 + 同意/拒绝检测。"""
        if not self.enabled_for_event(event):
            return
        try:
            message = self.normalize(event)
            self.save(message)
            if self.mode != "record_only" and self.learn_nicknames:
                candidate = extract_rule_candidate(message)
                if candidate and candidate.key == "preferred_name":
                    authority = self.authority.resolve(message.sender_id, message.group_key.split(":")[-1])
                    self.rules.create_request(message.group_key, candidate, authority)
            related_issuers = list(message.mentions)
            if message.reply_sender_id:
                related_issuers.append(message.reply_sender_id)
            if is_refusal(message.text):
                self.rules.reject_pending_for_target(
                    message.group_key,
                    message.sender_id,
                    related_issuers or None,
                )
            elif is_consent(message.text):
                self.rules.approve_pending_for_target(
                    message.group_key,
                    message.sender_id,
                    related_issuers or None,
                )
        except Exception:
            LOGGER.exception("Who Is Who 记录群消息失败")

    def prepare_turn(self, event: dict[str, Any]) -> PreparedTurn | None:
        """回复门控通过后：构建共享上下文 + 检测长期请求。失败返回 None 放行。"""
        if not self.enabled_for_event(event):
            return None
        try:
            message = self.normalize(event)
            self.save(message)
            request_id: int | None = None
            if self.mode == "full" and self.detect_commitments:
                candidate = extract_rule_candidate(message)
                if candidate and candidate.key != "preferred_name":
                    authority = self.authority.resolve(
                        message.sender_id, message.group_key.split(":")[-1]
                    )
                    state_id, status = self.rules.create_request(
                        message.group_key, candidate, authority
                    )
                    if status == "pending_response":
                        request_id = state_id
            context_text = self.context_builder.build(message, self.mode)
            return PreparedTurn(
                context_text=context_text,
                request_id=request_id,
                topic_id=message.topic_id,
                group_key=message.group_key,
            )
        except Exception:
            LOGGER.exception("Who Is Who 准备上下文失败，已放行原请求")
            return None

    def finish_turn(self, event: dict[str, Any], reply_text: str, prepared: PreparedTurn) -> None:
        """AI 回复落地后：记录 bot 消息 + 承诺裁决。"""
        try:
            text = str(reply_text or "").strip()
            if not text:
                return
            bot_message = NormalizedMessage(
                message_id=f"bot_{uuid.uuid4().hex}",
                group_key=prepared.group_key,
                sender_id=self.bot_qq or "bot",
                sender_name="ATRI",
                text=text,
                timestamp=int(time.time()),
                topic_id=prepared.topic_id,
                role="bot",
            )
            self.save(bot_message)
            if prepared.request_id is not None:
                self.rules.resolve_ai_response(
                    prepared.request_id, ai_accepted(text), bot_message.message_id
                )
        except Exception:
            LOGGER.exception("Who Is Who 记录 AI 回复失败")

    # ── 命令 ──

    def handle_command(self, event: dict[str, Any], text: str) -> str | None:
        """处理 /wiw 子命令；非 wiw 命令返回 None。"""
        stripped = strip_address_prefix(str(text or "").strip())
        lowered = stripped.casefold()
        if not lowered.startswith("/wiw"):
            return None
        if not self.enabled_for_event(event):
            return "Who Is Who 当前未在此群启用。"
        parts = stripped.split()
        sub = parts[1].casefold() if len(parts) >= 2 else ""
        args = parts[2:]
        group_key = self._group_key(event)
        group_id = str(event.get("group_id") or "")
        user_id = str(event.get("user_id") or "")
        authority = self.authority.resolve(user_id, group_id)

        if sub in {"", "help"}:
            return (
                "Who Is Who 指令：\n"
                "/wiw status - 查看当前群状态\n"
                "/wiw prefer <称呼> - 设置本人偏好称呼\n"
                "/wiw forget - 删除本人偏好称呼\n"
                "/wiw name <用户ID> <称呼> - 管理员设置成员称呼\n"
                "/wiw unname <用户ID> - 管理员删除成员称呼\n"
                "/wiw context - 查看本次注入上下文（管理员）\n"
                "/wiw rules - 查看当前规则与承诺（管理员）\n"
                "/wiw clear confirm - 清空当前群插件数据（管理员）"
            )
        if sub == "status":
            return (
                f"Who Is Who 已启用。\n运行模式：{self.mode}\n"
                f"你的有效优先级：{authority.priority}\n"
                f"权限来源：{', '.join(authority.sources) or 'default'}\n"
                "实时流程不会额外调用一次 AI。"
            )
        if sub == "prefer":
            return self._command_prefer(event, group_key, user_id, authority, args)
        if sub == "forget":
            return self._command_forget(group_key, user_id, authority)
        if sub == "name":
            return self._command_name(event, group_key, user_id, authority, args)
        if sub == "unname":
            return self._command_unname(group_key, user_id, authority, args)
        if sub == "context":
            if not authority.can("view_debug_context"):
                return "你没有查看调试上下文的权限。"
            message = self.normalize(event)
            return self.context_builder.build(message, self.mode)
        if sub == "rules":
            if not authority.can("manage_memory"):
                return "你没有查看状态账本的权限。"
            states = self.db.list_states(group_key, 20)
            if not states:
                return "当前群还没有规则或承诺记录。"
            lines = ["当前群最近的规则与承诺："]
            for item in states:
                lines.append(
                    f"#{item['id']} [{item['status']}/{item['scope']}] "
                    f"{item['content']}（优先级 {item['priority']}）"
                )
            return "\n".join(lines)
        if sub == "clear":
            if not authority.can("manage_memory"):
                return "你没有清空群级记忆的权限。"
            confirm = args[0].casefold() if args else ""
            if confirm not in {"confirm", "确认"}:
                return (
                    "此操作会永久删除当前群的身份、消息、话题和承诺数据。"
                    "如需继续，请发送 /wiw clear confirm"
                )
            self.db.clear_group(group_key)
            return "已清空当前群的 Who Is Who 数据，此操作不可恢复。"
        return "未知的 /wiw 子命令，发送 /wiw help 查看指令列表。"

    def _command_prefer(
        self,
        event: dict[str, Any],
        group_key: str,
        user_id: str,
        authority: Authority,
        args: list[str],
    ) -> str:
        name = " ".join(args).strip()[:20] if args else ""
        if not name:
            return "用法：/wiw prefer <称呼>。称呼不能为空。"
        candidate = RuleCandidate(
            kind="preference",
            key="preferred_name",
            content=f"成员希望被称为“{name}”",
            issuer_id=user_id,
            target_id=user_id,
            scope="user",
            topic_id=None,
            source_message_id="command",
            metadata={"value": name},
        )
        _, status = self.rules.create_request(group_key, candidate, authority)
        if status != "active":
            return "该成员称呼由插件管理员设置，你不能自行覆盖。"
        return f"已记录：在本群中你希望被称为“{name}”。"

    def _command_forget(self, group_key: str, user_id: str, authority: Authority) -> str:
        existing = self.db.get_active_conflict(group_key, "preferred_name", "user", user_id, None)
        self_priority = (
            SELF_PREFERENCE_PRIORITY if self.rules.self_preference_first else authority.priority
        )
        if existing and existing["priority"] > self_priority:
            return "该称呼由插件管理员设置，你不能自行删除。"
        self.db.supersede_states(group_key, "preferred_name", "user", user_id, self_priority, True)
        self.db.set_preferred_name(group_key, user_id, "")
        return "已删除你在本群的偏好称呼。"

    def _command_name(
        self,
        event: dict[str, Any],
        group_key: str,
        user_id: str,
        authority: Authority,
        args: list[str],
    ) -> str:
        if "plugin_admin" not in authority.sources:
            return "只有插件管理员（bot 主人）可以设置其他成员称呼。"
        if len(args) < 2:
            return "用法：/wiw name <用户ID> <称呼>"
        target_id = args[0]
        name = " ".join(args[1:]).strip()[:20]
        if not name:
            return "称呼不能为空。"
        candidate = RuleCandidate(
            kind="preference",
            key="preferred_name",
            content=f"插件管理员将该成员的称呼设置为“{name}”",
            issuer_id=user_id,
            target_id=target_id,
            scope="user",
            topic_id=None,
            requires_consent=True,
            source_message_id="command",
            metadata={"value": name},
        )
        _, status = self.rules.create_request(group_key, candidate, authority)
        if status != "active":
            return "只有插件管理员可以直接决定其他成员的称呼。"
        return f"已将该成员在本群的称呼设置为“{name}”。"

    def _command_unname(
        self,
        group_key: str,
        user_id: str,
        authority: Authority,
        args: list[str],
    ) -> str:
        if "plugin_admin" not in authority.sources:
            return "只有插件管理员（bot 主人）可以删除其他成员称呼。"
        if not args:
            return "用法：/wiw unname <用户ID>"
        target_id = args[0]
        self.db.clear_preferred_name_states(group_key, target_id, PLUGIN_ADMIN_PRIORITY)
        self.db.set_preferred_name(group_key, target_id, "")
        return "已删除该成员在本群的偏好称呼。"

    def shutdown(self) -> None:
        try:
            self.db.close()
        except Exception:
            LOGGER.exception("Who Is Who 数据库关闭失败")


_SERVICE: WhoIsWhoService | None = None


def get_who_is_who_service(config: Any) -> WhoIsWhoService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = WhoIsWhoService(config)
    return _SERVICE
