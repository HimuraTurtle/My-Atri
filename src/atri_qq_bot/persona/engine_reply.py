from __future__ import annotations

from .engine_support import PersonaEngineMixin


class EngineReplyMixin(PersonaEngineMixin):
    async def reply(
        self,
        conversation_id: str,
        user_text: str,
        nickname: str | None = None,
        profile_id: str | None = None,
        observed: bool = False,
        tool_context: Any | None = None,
        reply_focus: GroupReplyFocus | None = None,
        context_system: str | None = None,
    ) -> str:
        # 主流程遵循“清洗输入 -> 记录记忆 -> 确定性分支/安全兜底
        # -> 组织模型上下文 -> 校验输出”，便于定位每个阶段的责任。
        self._pending_reply_failures.pop(conversation_id, None)
        self._turn_tool_calls.pop(conversation_id, None)
        self._turn_contradictions.pop(conversation_id, None)
        clean_text = user_text.strip()
        if not clean_text:
            clean_text = "（用户发来了一条空消息）"

        profile_id = profile_id or conversation_id
        serious_requested = _requests_serious_mode(clean_text)
        if serious_requested:
            self._activate_serious_mode(conversation_id, profile_id)
        actor_id = _user_id_from_profile_id(profile_id)
        is_owner = _is_owner_id(actor_id, self.config.owner_qqs)
        command_reply = self._handle_affection_command(profile_id, clean_text, is_owner)
        if command_reply:
            self._remember(conversation_id, clean_text, command_reply, nickname)
            return command_reply

        if not observed:
            self.memory.observe_user(profile_id, clean_text, nickname=nickname, is_owner=is_owner)
            if profile_id != conversation_id:
                self.memory.observe_user(
                    conversation_id,
                    clean_text,
                    nickname=nickname,
                    is_owner=is_owner,
                )
        profile = self.memory.profile(profile_id)
        context_profile = (
            self.memory.profile(conversation_id) if profile_id != conversation_id else None
        )

        if serious_requested and _is_serious_only_message(clean_text):
            serious_reply = "嗯，我切回正常中文。抽象梗先收着，后面先按当前话题认真说。"
            self._remember(conversation_id, clean_text, serious_reply, nickname)
            return serious_reply

        iteration_decision = judge_correction(clean_text)
        if iteration_decision:
            self.memory.record_iteration_decision(
                profile_id,
                clean_text,
                iteration_decision.action,
                iteration_decision.reason,
            )
            if profile_id != conversation_id:
                self.memory.record_iteration_decision(
                    conversation_id,
                    clean_text,
                    iteration_decision.action,
                    iteration_decision.reason,
                )

        repair_mode = _needs_comfort_repair_mode(clean_text, iteration_decision)
        if repair_mode:
            self._activate_serious_mode(conversation_id, profile_id)

        visual_fail_safe_context = _visual_fail_safe_context(tool_context)
        if visual_fail_safe_context is not None:
            self._register_reply_failure(
                conversation_id,
                "视觉材料未取得足够可靠的识别结果",
            )
            self._remember_user_context(conversation_id, clean_text, nickname)
            # 识别失败不再静默：用户发图没人理会比承认看不清更伤体验。
            # 只声明识别不稳定，不描述任何画面内容，避免幻觉。
            honest_reply = (
                "呜……这张图我这次没识别稳定，不敢乱猜。"
                "可以再发一次试试，或者直接告诉我是啥呀？"
            )
            self._remember(conversation_id, clean_text, honest_reply, nickname)
            return honest_reply

        if self.config.ai_enabled:
            try:
                api_reply = await self._reply_with_guarded_api(
                    conversation_id,
                    clean_text,
                    nickname,
                    extra_system="\n\n".join(
                        part
                        for part in (
                            context_system,
                            COMFORT_REPAIR_PROMPT if repair_mode else None,
                        )
                        if part
                    )
                    or None,
                    profile_id=profile_id,
                    profile=profile,
                    context_profile=context_profile,
                    iteration_decision=iteration_decision,
                    tool_context=tool_context,
                    reply_focus=reply_focus,
                )
                human_pipeline = bool(
                    getattr(self.config, "human_reply_pipeline_enabled", True)
                )
                recent_messages = (
                    self._recent_conversation_messages(
                        conversation_id,
                        current_user_text=clean_text,
                    )
                    if human_pipeline
                    else []
                )
                if (
                    human_pipeline
                    and not _has_current_visual_context(tool_context)
                    and recent_messages
                    and _needs_history_grounding_review(api_reply)
                ):
                    reviewed_reply = await self._review_history_grounding(
                        clean_text,
                        api_reply,
                        recent_messages,
                    )
                    if reviewed_reply:
                        api_reply = reviewed_reply
                if (
                    human_pipeline
                    and _needs_hard_reality_repair(api_reply)
                ):
                    reality_prompt = (
                        "上一条候选回复把没有经过工具确认的现实动作、环境或实时状态"
                        "说成了已经发生的事实。请自然重写：保留对用户当前话题的回应，"
                        "但不要假装自己拿过物品、观察过现实环境或知道当前天气。"
                        "需要实时信息时应调用可用工具；本轮不需要实时信息时就删掉该断言。"
                        "不要解释内部检查过程，只输出最终回复。\n\n"
                        f"待修正回复：{api_reply}"
                    )
                    reality_reply = await self._reply_with_guarded_api(
                        conversation_id,
                        clean_text,
                        nickname,
                        extra_system="\n\n".join(
                            part
                            for part in (
                                context_system,
                                COMFORT_REPAIR_PROMPT if repair_mode else None,
                                reality_prompt,
                            )
                            if part
                        ),
                        profile_id=profile_id,
                        profile=profile,
                        context_profile=context_profile,
                        iteration_decision=iteration_decision,
                        tool_context=tool_context,
                        reply_focus=reply_focus,
                    )
                    if reality_reply and not _needs_hard_reality_repair(reality_reply):
                        api_reply = reality_reply
                    else:
                        raise RuntimeError("模型连续生成未经证实的现实断言")
                # 自洽审查（方案二）：回复把账本里已完成的动作说成待办时，
                # 注入账本事实重写一次。纯规则判定零成本；查无实据不拦，
                # 重写失败也不丢回复（漏拦放行的安全方向）。
                ledger_actions = (
                    self.memory.recent_completed_actions(conversation_id)
                    if human_pipeline
                    else []
                )
                ledger_facts = (
                    _completed_task_contradiction(api_reply, ledger_actions)
                    if ledger_actions
                    else ""
                )
                if ledger_facts:
                    ledger_rewrite_prompt = (
                        "上一条候选回复把已经完成的动作说成了还没做或待会儿再做，"
                        f"但这些动作其实已经完成：{ledger_facts}。"
                        "请自然重写：如实承认已经做完，并基于已完成的结果继续回应用户；"
                        "不要承诺稍后再做，也不要重新执行已经完成的动作。"
                        "不要解释内部检查过程，只输出最终回复。\n\n"
                        f"待修正回复：{api_reply}"
                    )
                    ledger_reply = await self._reply_with_guarded_api(
                        conversation_id,
                        clean_text,
                        nickname,
                        extra_system="\n\n".join(
                            part
                            for part in (
                                context_system,
                                COMFORT_REPAIR_PROMPT if repair_mode else None,
                                ledger_rewrite_prompt,
                            )
                            if part
                        ),
                        profile_id=profile_id,
                        profile=profile,
                        context_profile=context_profile,
                        iteration_decision=iteration_decision,
                        tool_context=tool_context,
                        reply_focus=reply_focus,
                    )
                    ledger_resolved = bool(ledger_reply) and not (
                        _completed_task_contradiction(ledger_reply, ledger_actions)
                    )
                    if ledger_reply:
                        api_reply = ledger_reply
                    self.note_turn_contradiction(
                        conversation_id, ledger_facts, ledger_resolved
                    )
                grounding_check = getattr(
                    tool_context,
                    "needs_grounding_repair",
                    None,
                )
                if (
                    human_pipeline
                    and callable(grounding_check)
                    and grounding_check(api_reply)
                ):
                    grounding_prompt = (
                        "上一条候选回复超出了本轮工具实际读取到的材料范围。"
                        "请只依据工具上下文中明确可见的信息重新回复；"
                        "没有读到正文、画面或声音时，不得声称自己看过、听过或读过。"
                        "自然说明信息边界，然后继续回应用户真正关心的内容，"
                        "不要解释内部工具、审核或重写过程。\n\n"
                        f"待修正回复：{api_reply}"
                    )
                    grounded_reply = await self._reply_with_guarded_api(
                        conversation_id,
                        clean_text,
                        nickname,
                        extra_system="\n\n".join(
                            part
                            for part in (
                                context_system,
                                COMFORT_REPAIR_PROMPT if repair_mode else None,
                                grounding_prompt,
                            )
                            if part
                        ),
                        profile_id=profile_id,
                        profile=profile,
                        context_profile=context_profile,
                        iteration_decision=iteration_decision,
                        tool_context=tool_context,
                        reply_focus=reply_focus,
                    )
                    if grounded_reply and not grounding_check(grounded_reply):
                        api_reply = grounded_reply
                    else:
                        raise RuntimeError("模型回复超出本轮工具证据范围")
                if not human_pipeline:
                    for _ in range(2):
                        if not self._needs_rewrite(conversation_id, clean_text, api_reply):
                            break
                        violations = _persona_violations(clean_text, api_reply)
                        rewrite_prompt = (
                            f"{REWRITE_PROMPT}\n\n"
                            f"{_rewrite_instruction(clean_text)}\n\n"
                            f"人设校验问题：{'; '.join(violations) if violations else '回复不够具体或有重复风险'}\n\n"
                            f"不合格回复：{api_reply}"
                        )
                        rewritten = await self._reply_with_guarded_api(
                            conversation_id,
                            clean_text,
                            nickname,
                            extra_system="\n\n".join(
                                part
                                for part in (
                                    context_system,
                                    COMFORT_REPAIR_PROMPT if repair_mode else None,
                                    rewrite_prompt,
                                )
                                if part
                            ),
                            profile_id=profile_id,
                            profile=profile,
                            context_profile=context_profile,
                            iteration_decision=iteration_decision,
                            tool_context=tool_context,
                            reply_focus=reply_focus,
                        )
                        if rewritten:
                            api_reply = rewritten
                    if self._needs_rewrite(conversation_id, clean_text, api_reply):
                        violations = _persona_violations(clean_text, api_reply)
                        reason = (
                            "；".join(violations)
                            if violations
                            else "未满足直答、情绪支持、去重或回复结构要求"
                        )
                        raise RuntimeError(f"模型连续 3 次未通过回复质量检查：{reason}")
                if _looks_incomplete_reply(api_reply):
                    repaired_reply = _repair_incomplete_reply(api_reply)
                    if not repaired_reply:
                        fallback_reply = _persona_repair_fallback(
                            clean_text,
                            api_reply,
                            serious_mode=self._serious_mode_active(conversation_id),
                            conversation_id=conversation_id,
                        )
                        repaired_reply = _repair_incomplete_reply(fallback_reply)
                    if repaired_reply:
                        api_reply = repaired_reply
                api_reply = self._finalize_reply(
                    conversation_id,
                    clean_text,
                    api_reply,
                    strict_quality=not human_pipeline,
                )
                if api_reply:
                    self._remember(conversation_id, clean_text, api_reply, nickname)
                    return api_reply
                raise RuntimeError("模型返回空消息")
            except Exception as exc:
                self._register_reply_failure(
                    conversation_id,
                    f"{type(exc).__name__}: {exc}",
                )
        else:
            self._register_reply_failure(conversation_id, "聊天模型当前未启用")
        self._remember_user_context(conversation_id, clean_text, nickname)
        return ""

    def _handle_affection_command(
        self,
        profile_id: str,
        text: str,
        is_owner: bool,
    ) -> str | None:
        lowered = text.strip().lower()
        if not lowered.startswith("/affection"):
            return None
        if not is_owner:
            return "这个指令只给主人用。哼，后台感觉这种东西不能随便给别人拨来拨去。"

        user_id = _user_id_from_profile_id(profile_id)
        target_id = f"private:{user_id}" if user_id is not None else profile_id
        parts = lowered.split()
        action = parts[1] if len(parts) >= 2 else "get"

        if action == "get":
            return self.memory.affection_summary(target_id, is_owner=True)
        if action == "reset":
            return self.memory.reset_affection(target_id, is_owner=True)
        if action == "set":
            value = _parse_affection_set_value(text)
            if value is None:
                return "给我一个能看懂的感觉值啦。比如偏低、普通、偏高，或者用你习惯的设置方式。"
            return self.memory.set_affection(target_id, value, is_owner=True)
        return "我看懂这是调整关系感觉的指令，但这个动作不认识。你可以用查询、调整或重置。"

