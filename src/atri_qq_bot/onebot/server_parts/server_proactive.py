from __future__ import annotations

import logging

from .server_support import OneBotServerMixin, _sync_server_globals

# pythonw 无控制台时 sys.stdout 为 None，裸 print 会抛异常杀死进程；
# 所有输出必须走 logging。
LOGGER = logging.getLogger("atri.onebot")


class OneBotProactiveMixin(OneBotServerMixin):
    async def idle_nudge_loop(self) -> None:
        while True:
            await asyncio.sleep(max(10, int(self.config.idle_check_seconds)))
            if getattr(self.config, "proactive_v2_enabled", False):
                continue
            if not self.config.idle_proactive_enabled:
                continue

            websocket = self._first_active_websocket()
            if websocket is None:
                continue

            for conversation_id, target in self.reply_engine.due_idle_targets():
                user_id = target.get("user_id")
                if not user_id:
                    continue
                text = self.reply_engine.idle_nudge_text(conversation_id)
                try:
                    await self.call_action(
                        websocket,
                        "send_private_msg",
                        {"user_id": user_id, "message": text},
                    )
                    self.reply_engine.mark_idle_nudged(conversation_id)
                except Exception as exc:
                    LOGGER.warning("闲置提醒发送失败已跳过：%s", exc)
                    break

    async def morning_greeting_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            if getattr(self.config, "proactive_v2_enabled", False):
                continue
            if not self.config.morning_greeting_enabled:
                continue

            websocket = self._first_active_websocket()
            if websocket is None:
                continue

            try:
                due_targets = self.reply_engine.due_morning_targets()
            except Exception as exc:
                LOGGER.warning("早安问候调度跳过：%s", exc)
                continue

            for conversation_id, target in due_targets:
                user_id = target.get("user_id")
                if not user_id:
                    continue
                text = self.reply_engine.morning_greeting_text()
                try:
                    await self.call_action(
                        websocket,
                        "send_private_msg",
                        {"user_id": user_id, "message": text},
                    )
                    self.reply_engine.mark_morning_greeted(conversation_id)
                    LOGGER.info("早安问候已发送给 %s。", user_id)
                except Exception as exc:
                    LOGGER.warning("早安问候发送失败：%s", exc)
                    break

    async def proactive_v2_loop(self) -> None:
        while True:
            policy = load_proactive_policy()
            await asyncio.sleep(max(15, int(policy.get("check_seconds", 60))))
            if not getattr(self.config, "proactive_v2_enabled", False):
                continue
            if not policy.get("enabled", True):
                continue

            websocket = self._first_active_websocket()
            if websocket is None:
                continue
            try:
                due_events = self.reply_engine.memory.due_personal_events(
                    getattr(self.config, "owner_qqs", ()),
                )
            except Exception as exc:
                LOGGER.warning("个人日程调度跳过：%s", exc)
                due_events = []
            for conversation_id, target, personal_event in due_events:
                try:
                    text = _personal_event_message(personal_event)
                    await self.call_action(
                        websocket,
                        "send_private_msg",
                        {"user_id": target["user_id"], "message": text},
                    )
                    self.reply_engine.memory.mark_personal_event_sent(
                        conversation_id,
                        str(personal_event.get("id") or ""),
                    )
                    self.reply_engine.record_bot_reply(conversation_id, text)
                    LOGGER.info("个人日程提醒已发送给 %s。", target["user_id"])
                except Exception as exc:
                    LOGGER.warning("个人日程提醒发送失败：%s", exc)
            self.proactive_planner.update_owner_qqs(getattr(self.config, "owner_qqs", ()))
            try:
                due_plans = self.proactive_planner.due_plans(policy)
            except Exception as exc:
                LOGGER.warning("主动 V2 调度跳过：%s", exc)
                continue

            for plan in due_plans:
                message_type = str(plan.target.get("message_type") or "private")
                user_id = plan.target.get("user_id")
                group_id = plan.target.get("group_id")
                if message_type == "group" and not group_id:
                    continue
                if message_type != "group" and not user_id:
                    continue
                result = await self.reply_engine.generate_proactive_message(
                    plan.conversation_id,
                    plan.event_type,
                    policy,
                )
                text = str(result.get("text") or "").strip()
                if not text:
                    if result.get("error"):
                        LOGGER.warning(
                            "主动 V2 跳过 %s：%s",
                            plan.conversation_id,
                            result["error"],
                        )
                    continue
                try:
                    message: str | list[dict[str, Any]] = text
                    sent_text = text
                    if message_type == "group":
                        await self.call_action(
                            websocket,
                            "send_group_msg",
                            {"group_id": group_id, "message": message},
                        )
                    else:
                        await self.call_action(
                            websocket,
                            "send_private_msg",
                            {"user_id": user_id, "message": message},
                        )
                    self.reply_engine.record_bot_reply(plan.conversation_id, sent_text)
                    timezone = safe_zoneinfo(str(policy.get("timezone") or "Asia/Shanghai"))
                    local_now = datetime.now(timezone)
                    self.reply_engine.memory.mark_proactive_sent(
                        plan.conversation_id,
                        plan.event_type,
                        sent_text,
                        str(result.get("source") or "unknown"),
                        local_now.date().isoformat(),
                        now=local_now.timestamp(),
                    )
                    LOGGER.info(
                        "主动 V2 已发送至 %s:%s；type=%s source=%s。",
                        message_type,
                        group_id or user_id,
                        plan.event_type,
                        result.get("source"),
                    )
                    if result.get("error"):
                        LOGGER.warning("主动 V2 生成回退：%s", result["error"])
                except Exception as exc:
                    LOGGER.warning("主动 V2 发送失败：%s", exc)
                    break

    def proactive_status(self) -> dict[str, Any]:
        policy = load_proactive_policy()
        self.proactive_planner.update_owner_qqs(getattr(self.config, "owner_qqs", ()))
        return {
            "feature_enabled": bool(getattr(self.config, "proactive_v2_enabled", False)),
            "policy": policy,
            "schedule": self.proactive_planner.status(policy),
        }

    async def preview_proactive(
        self,
        event_type: str,
        target_id: int | None = None,
        scope: str = "private",
    ) -> dict[str, str]:
        policy = load_proactive_policy()
        candidates = self.reply_engine.memory.proactive_candidates(
            getattr(self.config, "owner_qqs", ()),
            bool(policy.get("owner_only", False)),
            include_groups=bool(policy.get("group_enabled", False)),
        )
        scope = "group" if scope == "group" else "private"
        if target_id:
            conversation_id = f"{scope}:{int(target_id)}"
        elif matching := [item for item in candidates if item[1].get("message_type") == scope]:
            conversation_id = matching[0][0]
        else:
            raise ValueError(f"没有可预览的{'群聊' if scope == 'group' else '私聊'}目标")
        result = await self.reply_engine.generate_proactive_message(
            conversation_id,
            event_type,
            policy,
        )
        return {"conversation_id": conversation_id, "event_type": event_type, **result}

    async def meme_learning_loop(self) -> None:
        """定时抓取热搜并用模型精选成梗库；失败只记日志，不影响其他任务。"""

        import logging

        from ...memes import refresh_meme_library

        logger = logging.getLogger("atri.onebot")
        interval_seconds = max(
            600, int(getattr(self.config, "meme_learning_interval_hours", 6) * 3600)
        )
        # 等服务器完全启动并接入 NapCat 后再刷首轮，避免和启动初始化抢资源。
        await asyncio.sleep(45)
        while True:
            if getattr(self.config, "meme_learning_enabled", True):
                try:
                    summary = await refresh_meme_library(self.config)
                    logger.info("热梗库刷新完成：%s", summary)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("热梗库刷新失败：%s", exc)
            await asyncio.sleep(interval_seconds)

    async def group_proactive_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, int(self.config.group_proactive_check_seconds)))
            if (
                getattr(self.config, "proactive_v2_enabled", False)
                and load_proactive_policy().get("group_enabled", False)
            ):
                continue
            if not self.config.group_proactive_enabled:
                continue

            websocket = self._first_active_websocket()
            if websocket is None:
                continue

            for conversation_id, target in self.reply_engine.due_group_targets():
                group_id = target.get("group_id")
                if not group_id:
                    continue
                text = self.reply_engine.group_nudge_text(conversation_id)
                try:
                    await self.call_action(
                        websocket,
                        "send_group_msg",
                        {"group_id": group_id, "message": text},
                    )
                    self.reply_engine.mark_group_proactive(conversation_id)
                    self.reply_engine.record_bot_reply(conversation_id, text)
                    print(f"[onebot] Group proactive nudge sent to {group_id}.")
                except Exception as exc:
                    print(f"[onebot] Group proactive send failed: {exc}")
                    break

    def _first_active_websocket(self) -> Any | None:
        for websocket in list(self._active_websockets):
            return websocket
        return None

    def _log_message_decision(
        self,
        event: dict[str, Any],
        plain_text: str,
        should_send_reply: bool,
    ) -> None:
        with contextlib.suppress(Exception):
            self._event_log.parent.mkdir(parents=True, exist_ok=True)
            preview = plain_text.replace("\n", " ")[:120]
            line = (
                f"{_now_text()} mode={self.config.reply_mode} "
                f"type={event.get('message_type')} group={event.get('group_id')} "
                f"user={event.get('user_id')} reply={should_send_reply} text={preview}\n"
            )
            with self._event_log.open("a", encoding="utf-8") as file:
                file.write(line)

    def _log_voice_event(
        self,
        event: dict[str, Any],
        status: str,
        *,
        message: OutgoingMessage | None = None,
        result: Any | None = None,
        error: Any = "",
        elapsed_ms: float | None = None,
    ) -> None:
        # Pytest uses synthetic QQ ids and temporary audio paths. Keeping those
        # events out of the operator log makes production failure rates usable.
        if os.getenv("PYTEST_CURRENT_TEST"):
            return
        with contextlib.suppress(Exception):
            error_text = str(error or "").strip()
            if not error_text and isinstance(error, BaseException):
                error_text = f"{type(error).__name__}: {error!r}"
            payload = {
                "time": _now_text(),
                "status": status,
                "message_type": event.get("message_type"),
                "group_id": event.get("group_id"),
                "user_id": event.get("user_id"),
                "source": str(getattr(result, "source", "") or ""),
                "file": str(
                    getattr(result, "audio_path", "")
                    or (message.content if message is not None else "")
                ),
                "retcode": result.get("retcode") if isinstance(result, dict) else None,
                "error": error_text[:500],
                "elapsed_ms": round(float(elapsed_ms), 1) if elapsed_ms is not None else None,
            }
            self._voice_event_log.parent.mkdir(parents=True, exist_ok=True)
            with self._voice_event_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _record_reply_failure(
        self,
        event: dict[str, Any],
        *,
        turn_id: str,
        conversation_id: str,
        user_text: str,
        model_reply: str,
        reason: str,
        tool_context: Any | None = None,
        reply_focus: Any | None = None,
        quoted_message_id: str = "",
        model_type: str = "chat",
        model_name: str = "",
    ) -> None:
        """把失败写入诊断与评测旁路，不生成或发送任何 QQ 消息。"""
        safe_reason = str(reason or "未知回复错误").strip()[:800]
        LOGGER.error(
            "回复失败，已保持用户侧静默：conversation=%s error=%s",
            conversation_id,
            safe_reason,
        )
        self._log_reply_event(
            event,
            conversation_id=conversation_id,
            user_text=user_text,
            model_reply=model_reply,
            delivered_reply="",
            material_read_level=str(getattr(tool_context, "read_level", "") or ""),
            reply_focus_source=str(getattr(reply_focus, "source", "") or ""),
            reply_focus_text=str(getattr(reply_focus, "focus_text", "") or ""),
            quoted_message_id=quoted_message_id,
            status="failed",
            failure_reason=safe_reason,
        )
        if self.insights is None:
            return
        sample = EvaluationSample(
            turn_id=turn_id,
            conversation_id=conversation_id,
            group_id=str(event.get("group_id") or ""),
            question=user_text,
            answer=model_reply,
            context=context_from_tool_context(tool_context),
            model_type=model_type,
            model_name=model_name,
        )
        submit_failure = getattr(self.insights, "submit_evaluation_failure", None)
        if callable(submit_failure):
            submit_failure(sample, safe_reason)

    def _log_reply_event(
        self,
        event: dict[str, Any],
        *,
        conversation_id: str,
        user_text: str,
        model_reply: str,
        delivered_reply: str,
        voice_mode: str = "",
        material_read_level: str = "",
        reply_focus_source: str = "",
        reply_focus_text: str = "",
        quoted_message_id: str = "",
        status: str = "delivered",
        failure_reason: str = "",
    ) -> None:
        with contextlib.suppress(Exception):
            payload = {
                "time": _now_text(),
                "conversation_id": conversation_id,
                "message_type": event.get("message_type"),
                "group_id": event.get("group_id"),
                "user_id": event.get("user_id"),
                "user_text": str(user_text or "")[:800],
                "model_reply": str(model_reply or "")[:1200],
                "delivered_reply": str(delivered_reply or "")[:1200],
                "voice_mode": voice_mode,
                "material_read_level": material_read_level,
                "reply_focus_source": reply_focus_source,
                "reply_focus_text": str(reply_focus_text or "")[:400],
                "quoted_message_id": str(quoted_message_id or ""),
                "status": str(status or "delivered"),
                "failure_reason": str(failure_reason or "")[:800],
            }
            self._reply_event_log.parent.mkdir(parents=True, exist_ok=True)
            with self._reply_event_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")

