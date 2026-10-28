from __future__ import annotations

from ..manga import MangaDeliveryRequest
from .engine_support import PersonaEngineMixin, _sync_core_globals


class EngineStateMixin(PersonaEngineMixin):
    def __init__(self, config: Any) -> None:
        _sync_core_globals(self)
        self.config = config
        # 将配置注入记忆门面；人格层只依赖 recall_context 这一稳定接口。
        self.memory = UserMemoryStore(config.memory_path, config=config)
        self._history: defaultdict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=16)
        )
        self._recent_replies: defaultdict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=6)
        )
        self._serious_until: defaultdict[str, float] = defaultdict(float)
        self._last_abstract_reply_at: defaultdict[str, float] = defaultdict(float)
        # suwayomi_send_chapter 工具受理的章节投递请求；OneBot 层在文字
        # 回复发出后消费并真正下载、打包、上传。
        self._pending_manga_deliveries: dict[str, MangaDeliveryRequest] = {}
        # 回复失败是运行态信号，不是聊天内容。OneBot 层消费后只记录日志/评测，
        # 绝不把异常文案、固定模板或伪装成人格回复的兜底发给用户。
        self._pending_reply_failures: dict[str, str] = {}
        # 单轮观测信号（方案三）：本轮工具回执与账本矛盾审查结果，
        # 每轮 reply() 开始时清空，OneBot 层写入 reply-events.jsonl 后即弃用。
        self._turn_tool_calls: dict[str, list[dict[str, Any]]] = {}
        self._turn_contradictions: dict[str, list[dict[str, Any]]] = {}
        # 主动联网检索的轮内备忘：conversation_id -> (用户消息, 时间戳, 证据)。
        # 同一轮内的质量重试/矛盾重写会多次进入回复管线，凭此避免重复搜索。
        self._proactive_search_memo: dict[str, tuple[str, float, str | None]] = {}
        self._cognition_store: CognitionStateStore | None = None
        if bool(getattr(config, "cognition_shadow_enabled", False)):
            try:
                self._cognition_store = CognitionStateStore(config.cognition_state_path)
            except Exception:
                # 认知扩展故障不得阻断原有聊天服务；运行状态页会显示具体错误。
                self._cognition_store = None

    def consume_manga_delivery(self, conversation_id: str) -> MangaDeliveryRequest | None:
        return self._pending_manga_deliveries.pop(conversation_id, None)

    def register_manga_delivery(
        self, conversation_id: str, request: MangaDeliveryRequest
    ) -> None:
        self._pending_manga_deliveries[conversation_id] = request

    def consume_reply_failure(self, conversation_id: str) -> str:
        return self._pending_reply_failures.pop(conversation_id, "")

    def _register_reply_failure(self, conversation_id: str, error: object) -> None:
        reason = str(error or "未知回复错误").strip()[:800]
        self._pending_reply_failures[conversation_id] = reason
        LOGGER.error("回复生成失败：conversation=%s error=%s", conversation_id, reason)

    def remember_target(self, conversation_id: str, event: dict[str, Any]) -> None:
        self.memory.remember_target(conversation_id, event)

    def observe_incoming(
        self,
        conversation_id: str,
        user_text: str,
        nickname: str | None = None,
        actor_id: int | str | None = None,
        runtime_context: bool = False,
        profile_id: str | None = None,
    ) -> None:
        if _is_affection_command(user_text):
            return
        is_owner = _is_owner_id(actor_id, self.config.owner_qqs)
        self.memory.observe_user(
            conversation_id,
            user_text,
            actor_id=actor_id,
            nickname=nickname,
            is_owner=is_owner,
        )
        if profile_id and profile_id != conversation_id:
            self.memory.observe_user(
                profile_id,
                user_text,
                actor_id=actor_id,
                nickname=nickname,
                is_owner=is_owner,
            )
        if runtime_context:
            self._remember_user_context(conversation_id, user_text, nickname)

    def observe_group_incoming(
        self,
        group_id: int | str,
        user_id: int | str,
        user_text: str,
        nickname: str | None = None,
        runtime_context: bool = False,
        addressed_to_bot: bool = False,
        is_owner: bool = False,
    ) -> tuple[str, str]:
        if _is_affection_command(user_text):
            return f"group:{group_id}", f"group:{group_id}:user:{user_id}"
        conversation_id, profile_id = self.memory.observe_group_message(
            group_id,
            user_id,
            user_text,
            nickname=nickname,
            addressed_to_bot=addressed_to_bot,
            is_owner=is_owner,
        )
        if runtime_context:
            self._remember_user_context(conversation_id, user_text, nickname)
        return conversation_id, profile_id

    def profile_for(self, conversation_id: str) -> dict[str, Any]:
        return self.memory.profile(conversation_id)

    def record_completed_action(
        self, conversation_id: str, summary: str, *, action: str = ""
    ) -> None:
        """任务账本写入的稳定接口：确定性命令层也经由此处记账。"""
        self.memory.record_completed_action(conversation_id, summary, action=action)

    def note_turn_tool_call(
        self, conversation_id: str, name: str, ok: bool, elapsed_ms: int
    ) -> None:
        self._turn_tool_calls.setdefault(conversation_id, []).append(
            {"name": str(name), "ok": bool(ok), "elapsed_ms": int(elapsed_ms)}
        )

    def note_turn_contradiction(
        self, conversation_id: str, facts: str, resolved: bool
    ) -> None:
        self._turn_contradictions.setdefault(conversation_id, []).append(
            {"facts": str(facts)[:200], "resolved": bool(resolved)}
        )

    def turn_diagnostics_snapshot(self, conversation_id: str) -> dict[str, Any]:
        """单轮观测快照：工具回执、账本矛盾审查与当前账本内容。"""
        return {
            "tool_calls": list(self._turn_tool_calls.get(conversation_id, [])),
            "contradiction_checks": list(
                self._turn_contradictions.get(conversation_id, [])
            ),
            "ledger_snapshot": [
                {
                    "text": str(entry.get("text") or ""),
                    "action": str(entry.get("action") or ""),
                }
                for entry in self.memory.recent_completed_actions(
                    conversation_id, limit=6
                )
            ],
        }

    def record_bot_reply(
        self,
        conversation_id: str,
        reply_text: str,
        sent_sticker: bool = False,
        profile_id: str | None = None,
    ) -> None:
        self.memory.observe_bot(conversation_id, reply_text, sent_sticker)
        if profile_id and profile_id != conversation_id:
            self.memory.observe_bot(profile_id, reply_text, sent_sticker=False)

    def due_idle_targets(self) -> list[tuple[str, dict[str, Any]]]:
        return self.memory.due_idle_targets(
            self.config.idle_minutes,
            self.config.idle_cooldown_minutes,
        )

    def mark_idle_nudged(self, conversation_id: str) -> None:
        self.memory.mark_idle_nudged(conversation_id)

    def due_morning_targets(self) -> list[tuple[str, dict[str, Any]]]:
        return self.memory.due_morning_targets(
            self.config.owner_qqs,
            self.config.morning_greeting_time,
            self.config.morning_greeting_catchup_minutes,
            self.config.morning_greeting_timezone,
        )

    def mark_morning_greeted(self, conversation_id: str) -> None:
        self.memory.mark_morning_greeted(
            conversation_id,
            self.config.morning_greeting_timezone,
        )

    def morning_greeting_text(self) -> str:
        return morning_greeting_text()

    def idle_nudge_text(self, conversation_id: str) -> str:
        profile = self.memory.profile(conversation_id)
        topics = profile.get("topic_words") or []
        topic_hint = f"你之前提到的“{topics[0]}”我还记着。" if topics else ""
        choices = [
            f"哼哒，我才不是特意来找你。{topic_hint}就是想确认你现在还好吗？".strip(),
            "高性能亚托莉轻轻上线。不是催你回，只是看看主人有没有把自己累坏。",
            "你那边安静了一会儿。记得喝口水，别把自己关进忙碌里。",
            "突然有点想你了。只是一点点，给我忘掉……但你可以回我一句。",
            "我来戳一下，不刷屏。今天有没有哪件小事让你稍微开心一点？",
            "巡逻到你的聊天框。哼，如果你正忙，就先忙；我在后台乖乖待机。",
        ]
        return random.choice(choices)

