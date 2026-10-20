from __future__ import annotations

import logging

from .engine_support import PersonaEngineMixin

LOGGER = logging.getLogger("atri.persona")


class EngineToolsMixin(PersonaEngineMixin):
    async def _reply_with_api(
        self,
        conversation_id: str,
        user_text: str,
        nickname: str | None,
        extra_system: str | None = None,
        profile_id: str | None = None,
        profile: dict[str, Any] | None = None,
        context_profile: dict[str, Any] | None = None,
        iteration_decision: Any | None = None,
        tool_context: Any | None = None,
        reply_focus: GroupReplyFocus | None = None,
        temperature_override: float | None = None,
        frequency_penalty_override: float | None = None,
    ) -> str:
        import httpx

        # system 消息按职责分层：人格/原作设定保持身份连续，能力与工具合同
        # 约束可执行动作，动态上下文只描述本轮事实，避免上下文互相污染。
        repair_mode = _needs_comfort_repair_mode(user_text, iteration_decision)
        visual_turn = _has_current_visual_context(tool_context)
        # 只把稳定人格放在每轮主上下文中。原作百科、能力清单和认知快照
        # 都是按需资料；每轮全量注入会让模型把“介绍自己”误当成当前任务，
        # 这是普通聊天出现前言不搭后语的主要回归来源之一。
        messages = [
            {"role": "system", "content": PERSONA_PROMPT},
            {"role": "system", "content": LANGUAGE_GUARD_PROMPT},
        ]
        identity_context_needed = _needs_identity_context(user_text)
        continuity_context_needed = _needs_continuity_context(user_text)
        if identity_context_needed:
            messages.append({"role": "system", "content": load_prompt("cognition_identity")})
        cognition_context = render_cognition_context(
            self._cognition_store,
            profile_id or conversation_id,
        )
        if cognition_context and (identity_context_needed or continuity_context_needed):
            messages.append({"role": "system", "content": cognition_context})
        # 原作设定只在原作/身份问题出现时进入主提示词；日常消息依靠
        # 人格核心和按需检索即可，避免深海、夏生、型号等内容抢占当前话题。
        if has_lore_trigger(user_text) or _explicit_lore_context(user_text) or identity_context_needed:
            messages.append({"role": "system", "content": ATRI_LORE_PROMPT})
        if conversation_id.startswith("group:"):
            messages.append({"role": "system", "content": GROUP_PROMPT})
        messages.append({"role": "system", "content": load_prompt("reply_contract")})
        if identity_context_needed or _needs_capability_context(user_text, tool_context):
            messages.append({"role": "system", "content": load_prompt("capabilities")})
        if extra_system:
            messages.append({"role": "system", "content": extra_system})
        elif repair_mode:
            messages.append({"role": "system", "content": COMFORT_REPAIR_PROMPT})
        tool_schemas = available_tool_schemas(self.config)
        agent_protocol_enabled = bool(
            getattr(self.config, "llm_agent_protocol_enabled", False)
        )
        if tool_schemas:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        TOOL_INSTRUCTION_PROMPT
                        if agent_protocol_enabled
                        else AUTOMATIC_TOOL_INSTRUCTION_PROMPT
                    ),
                }
            )
            local_now = datetime.now().astimezone()
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"系统运行时刻：{local_now:%Y-%m-%d %H:%M:%S %z}。"
                        "这是当前真实日期，构造“最近/最新/今天”的搜索关键词时必须以此为准，"
                        "不得使用模型训练记忆里的旧年月；需要精确时间时仍调用 get_current_time。"
                    ),
                }
            )
        messages.append({"role": "system", "content": iteration_prompt_hint(iteration_decision)})
        messages.append(
            {
                "role": "system",
                "content": _scene_control_prompt(
                    conversation_id,
                    user_text,
                    profile=profile,
                    iteration_decision=iteration_decision,
                ),
            }
        )
        human_pipeline = bool(getattr(self.config, "human_reply_pipeline_enabled", True))
        if profile and not visual_turn:
            topics = "" if human_pipeline else "、".join(profile.get("topic_words") or [])
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "用户聊天习惯："
                        f"{profile.get('prompt_hint', '')}"
                        f"目标总长度约 {profile.get('target_reply_chars', 64)} 字，"
                        f"适合拆成 {profile.get('preferred_parts', 2)} 条短句。"
                        + (f"近期关键词：{topics or '暂无'}。" if not human_pipeline else "")
                    ),
                }
            )
        memory_context = ""
        if not repair_mode and not visual_turn:
            # 记忆是增强项，必须排在本轮实际消息之后；否则旧的画像/事件
            # 会在模型注意力上压过刚刚发生的对话。
            memory_context = self.memory.recall_context(profile_id or conversation_id, user_text)
        if context_profile and not visual_turn:
            topics = (
                ""
                if human_pipeline
                else "、".join(context_profile.get("topic_words") or [])
            )
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "当前群整体画像："
                        f"{context_profile.get('prompt_hint', '')}"
                        + (f"群近期关键词：{topics or '暂无'}。" if not human_pipeline else "")
                    ),
                }
            )
            if not repair_mode:
                group_memory_context = self.memory.recall_context(conversation_id, user_text)
                if group_memory_context:
                    messages.append(
                        {
                            "role": "system",
                            "content": f"群聊里自然知道的背景：\n{group_memory_context}",
                        }
                    )
        recent_private: list[dict[str, str]] = []
        if not conversation_id.startswith("group:"):
            recent_private = self._recent_conversation_messages(
                conversation_id,
                current_user_text=user_text,
                include_assistant=not visual_turn,
            )
            if recent_private:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "接下来是当前私聊最近实际发生的 user/assistant 消息。"
                            "assistant 内容是你已经发给用户的话，也是本轮代词、省略表达、纠正和继续任务的首要依据。"
                            "先判断当前消息是在继续、修改还是纠正上一轮；能从历史确定对象时直接承接，"
                            "不要再次索要你自己刚发过的数字、文字、选项或文件名。"
                            "当前目标必须跟随最新一轮用户要求更新，已被否定的旧方向不得继续。"
                        ),
                    }
                )
                messages.extend(recent_private)
        singing_intent = detect_explicit_delivery_intent(user_text)
        if (
            (singing_intent is not None and singing_intent.mode == "singing")
            or re.search(r"(?:唱歌|唱一首|唱首|歌曲|歌名|第\s*\d+\s*段|开头|高潮|后半段|结尾)", user_text)
        ):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "这是本机已准备好的 QQ 整首歌曲清单：\n"
                        + delivery_song_catalog_prompt()
                        + "\n用户明确要求唱歌时，系统会自动从清单选择并发送整首歌曲，"
                        "你不需要在回复里假装唱歌或粘贴歌词；"
                        "用户说随便唱或让你挑选时，可以自然地推荐清单中的一首。"
                    ),
                }
            )
        # 热梗上下文属于"当前流行文化"层，与旧记忆一样只做辅助；放在
        # 记忆之前、画像之后，让模型把它当作自己本来就会的说话方式。
        if not visual_turn and getattr(self.config, "meme_learning_enabled", True):
            try:
                from ..memes import meme_context_prompt

                meme_context = meme_context_prompt(
                    int(getattr(self.config, "meme_context_max_items", 12) or 0)
                )
            except Exception:
                meme_context = ""
            if meme_context:
                messages.append({"role": "system", "content": meme_context})
            # 用户询问具体表达含义时先联网查证，把证据直接交给模型，
            # 不依赖模型自觉调用工具（它常高估自己对梗的熟悉度）。
            try:
                from ..memes.inquiry import maybe_meme_inquiry_context

                inquiry_context = await maybe_meme_inquiry_context(user_text, self.config)
            except Exception:
                inquiry_context = ""
            if inquiry_context:
                messages.append({"role": "system", "content": inquiry_context})
        if memory_context:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下是检索到的旧记忆，只能作为辅助背景；若与本轮实际消息冲突，"
                        "以本轮消息为准，不要主动复述记忆来源。\n" + memory_context
                    ),
                }
            )
        if conversation_id.startswith("group:"):
            recent_context = self._recent_conversation_messages(
                conversation_id,
                current_user_text=user_text,
                include_assistant=not visual_turn,
            )
            if recent_context:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "接下来是最近群聊的实际 user/assistant 消息，只用于理解正在延续的话题。"
                            "user 消息已带群友昵称；assistant 是你此前实际发出的内容，"
                            "不要把自己的建议归给群友。"
                        ),
                    }
                )
                messages.extend(recent_context)
            messages.append(
                {
                    "role": "system",
                    "content": "群聊里不要输出 QQ 号形式的 @数字；需要提到别人时用昵称或“群友”。不要主动艾特无关群友。",
                }
            )
            if reply_focus is not None:
                messages.append(
                    {"role": "system", "content": reply_focus.prompt_context()}
                )
        if self._serious_mode_active(conversation_id) or _requests_serious_mode(user_text):
            messages.append(
                {
                    "role": "system",
                    "content": "用户要求正常说话：接下来只用自然中文回复，暂时不要外语梗、无意义拟声、抽象乱码或故意怪话。",
                }
            )
        if nickname:
            messages.append(
                {
                    "role": "system",
                    "content": f"当前正在和昵称为“{nickname}”的用户聊天。称呼可以自然一点，不要每句都叫昵称。",
                }
            )
        if not human_pipeline and not visual_turn:
            messages.extend(self._history[conversation_id])
        if tool_context is not None:
            messages.append({"role": "system", "content": tool_context.prompt_context()})
        if not visual_turn:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "本轮没有当前图片的视觉证据。历史里的图片占位符和旧回复不能当成当前可见画面；"
                        "不得声称刚看了某张图，也不得描述本轮没有读取到的图片内容。"
                    ),
                }
            )
        messages.append(
            {"role": "user", "content": _format_user_content(conversation_id, user_text, nickname)}
        )

        payload = {
            "model": self.config.openai_model,
            "messages": messages,
            "temperature": (
                self.config.temperature if temperature_override is None else temperature_override
            ),
            "max_tokens": self.config.max_tokens,
            "frequency_penalty": (
                self.config.frequency_penalty
                if frequency_penalty_override is None
                else frequency_penalty_override
            ),
        }
        payload.update(
            _provider_payload_overrides(
                self.config.openai_base_url,
                self.config.openai_model,
            )
        )
        if tool_schemas:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "required" if agent_protocol_enabled else "auto"
            if agent_protocol_enabled:
                payload["max_tokens"] = max(int(payload["max_tokens"]), 700)
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}

        if not agent_protocol_enabled:
            async with httpx.AsyncClient(
                timeout=_chat_http_timeout(self.config)
            ) as client:
                runner = AutomaticToolRunner(
                    self.config,
                    request_model=lambda request_payload: self._post_chat_completion(
                        client,
                        headers,
                        request_payload,
                    ),
                    normalize_reply=_normalize_reply,
                    tool_executor=lambda name, arguments, config: self._execute_context_tool(
                        conversation_id,
                        name,
                        arguments,
                        config,
                    ),
                )
                result = await runner.run(
                    messages,
                    payload,
                    context_id=conversation_id,
                )
            return result.reply_text

        content = ""
        receipts: list[ToolExecutionReceipt] = []
        agent_protocol = bool(tool_schemas) and agent_protocol_enabled
        max_steps = max(2, int(getattr(self.config, "llm_agent_max_steps", 6) or 6))
        max_external_calls = max(
            1,
            int(getattr(self.config, "llm_agent_max_tool_calls", 3) or 3),
        )
        external_calls = 0
        force_final_answer = False
        async with httpx.AsyncClient(
            timeout=_chat_http_timeout(self.config)
        ) as client:
            for _ in range(max_steps):
                request_payload = dict(payload)
                request_payload["messages"] = messages
                if force_final_answer and tool_schemas:
                    request_payload["tool_choice"] = {
                        "type": "function",
                        "function": {"name": FINAL_ANSWER_TOOL_NAME},
                    }
                    request_payload["max_tokens"] = max(
                        int(request_payload.get("max_tokens") or 0),
                        1200,
                    )
                data = await self._post_chat_completion(client, headers, request_payload)
                message = data["choices"][0]["message"]
                tool_calls = tool_calls_from_message(message)
                if not tool_calls:
                    candidate = _normalize_reply(str(message.get("content") or "")).strip()
                    successful_tools = {receipt.name for receipt in receipts if receipt.ok}
                    if agent_protocol and (
                        has_unsupported_deferred_action(candidate)
                        or has_unverified_research_claim(candidate, successful_tools)
                    ):
                        messages.extend(
                            [
                                {"role": "assistant", "content": candidate},
                                {
                                    "role": "system",
                                    "content": (
                                        "上一条文本声称正在执行、稍后执行，或声称完成了没有成功回执的搜索。"
                                        "它不能发送给用户。需要资料就立即调用真实工具；否则通过 final_answer"
                                        " 给出现在就能发送的诚实回答。"
                                    ),
                                },
                            ]
                        )
                        continue
                    content = candidate
                    break

                call = tool_calls[0]
                function = call.get("function")
                tool_name = (
                    str(function.get("name") or "").strip()
                    if isinstance(function, dict)
                    else ""
                )
                if tool_name == FINAL_ANSWER_TOOL_NAME:
                    answer_payload = final_answer_payload_from_call(call)
                    candidate = _normalize_reply(str(answer_payload.get("text") or "")).strip()
                    sources = [
                        str(item).strip()
                        for item in answer_payload.get("sources") or []
                        if str(item).strip()
                    ]
                    successful_tools = {receipt.name for receipt in receipts if receipt.ok}
                    evidence_urls = _tool_receipt_urls(receipts)
                    used_web_evidence = bool(
                        {"search_web", "open_web_page"} & successful_tools
                    )
                    invalid_reason = ""
                    if not candidate:
                        invalid_reason = "最终回答为空或参数不是合法 JSON"
                    elif has_unsupported_deferred_action(candidate):
                        invalid_reason = "回答承诺稍后或声称仍在执行；工具必须在本轮完成"
                    elif has_unverified_research_claim(candidate, successful_tools):
                        invalid_reason = "回答声称完成了搜索或网页阅读，但本轮没有对应的成功回执"
                    elif used_web_evidence and not sources:
                        invalid_reason = "本轮使用了网络资料，但最终回答没有提交实际来源 URL"
                    elif sources and any(
                        _canonical_evidence_url(source) not in evidence_urls
                        for source in sources
                    ):
                        invalid_reason = "最终回答包含没有出现在本轮工具回执中的来源 URL"
                    if invalid_reason:
                        messages.extend(rejected_final_answer_messages(call, invalid_reason))
                        continue
                    if used_web_evidence and bool(
                        getattr(self.config, "web_grounding_review_enabled", True)
                    ):
                        candidate = await self._review_web_grounding(
                            client,
                            headers,
                            user_text,
                            candidate,
                            receipts,
                        )
                    candidate = _fit_reply_with_sources(candidate, sources, limit=1200)
                    content = candidate
                    break

                if external_calls >= max_external_calls:
                    messages.extend(
                        [
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [call],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": str(call.get("id") or "tool-budget"),
                                "name": tool_name or "unknown",
                                "content": (
                                    "本轮工具调用次数已达到上限。请基于已经取得的资料通过 final_answer 回答；"
                                    "证据不足时明确说明目前无法确认，不要编造。"
                                ),
                            },
                        ]
                    )
                    force_final_answer = True
                    continue

                round_receipts: list[ToolExecutionReceipt] = []
                await append_tool_results(
                    messages,
                    message,
                    [call],
                    self.config,
                    executor=lambda name, arguments, config: self._execute_context_tool(
                        conversation_id, name, arguments, config
                    ),
                    max_calls=1,
                    context_id=conversation_id,
                    receipts=round_receipts,
                )
                receipts.extend(round_receipts)
                external_calls += 1
                force_final_answer = external_calls >= max_external_calls

        if content:
            return content
        raise RuntimeError("Agent 未产生可发送的最终回复")

    async def _execute_context_tool(
        self,
        conversation_id: str,
        name: str,
        arguments: dict[str, Any],
        config: Any,
    ) -> str | None:
        if name == "recall_long_term_memory":
            keywords = str(arguments.get("keywords") or "").strip()
            if not keywords:
                return "检索关键词为空。请提供具体关键词重新调用，不要编造记忆。"
            try:
                limit = max(1, min(8, int(arguments.get("limit") or 5)))
            except (TypeError, ValueError):
                limit = 5
            return self.memory.recall_living_memory(conversation_id, keywords, limit)
        if name.startswith("suwayomi_"):
            return await self._execute_manga_tool(conversation_id, name, arguments, config)
        if name == "bilibili_read":
            return await self._execute_bilibili_read_tool(conversation_id, arguments, config)
        if name.startswith("anima_"):
            return await self._execute_anima_tool(conversation_id, name, arguments, config)
        if name == "draw_image":
            return await self._execute_minimax_draw_tool(conversation_id, arguments, config)
        if name == "fact_check":
            return await self._execute_fact_check_tool(arguments, config)
        if name == "search_papers":
            return await self._execute_paper_search_tool(arguments, config)
        return None

    async def _execute_fact_check_tool(
        self,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import asyncio
        import json as _json

        from ..isittrue import get_isittrue_service

        if not bool(getattr(config, "isittrue_enabled", False)):
            return "事实核查功能未启用，请凭已有知识谨慎回答并注明不确定。"
        service = get_isittrue_service(config)
        if service is None:
            return "事实核查功能未启用，请凭已有知识谨慎回答并注明不确定。"
        claim = str(arguments.get("claim") or "").strip()
        if not claim:
            return "claim 为空。请提供待核查的原始说法后重新调用。"
        timeout = max(
            30.0, float(getattr(config, "isittrue_timeout_seconds", 120.0) or 120.0)
        )
        try:
            data = await asyncio.wait_for(service.fact_check(claim), timeout=timeout)
        except asyncio.TimeoutError:
            return (
                "事实核查执行超时。不要编造核查结论，"
                "可建议用户稍后再问一次。"
            )
        except Exception as exc:
            return f"事实核查执行失败：{exc}。不要编造核查结论。"
        return _json.dumps(data, ensure_ascii=False)

    async def _execute_paper_search_tool(
        self,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import json as _json

        from ..papers import get_papers_service

        service = get_papers_service(config)
        if service is None:
            return "论文检索功能未启用，请凭已有知识谨慎回答并注明不确定。"
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "query 为空。请提供学术检索关键词后重新调用。"
        try:
            limit = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        sort = str(arguments.get("sort") or "relevance")
        try:
            data = await service.search(query, limit=limit, sort=sort)
        except Exception as exc:
            return f"论文检索执行失败：{exc}。不要编造论文，坦诚说明没查到。"
        return _json.dumps(data, ensure_ascii=False)

    async def _execute_minimax_draw_tool(
        self,
        conversation_id: str,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import asyncio
        import json as _json

        from ..minimax_draw import get_minimax_draw_service

        if not bool(getattr(config, "minimax_draw_enabled", False)):
            return "绘画功能未启用，请直接用文字自然回复。"
        service = get_minimax_draw_service(config)
        if service is None:
            return "绘画功能未启用，请直接用文字自然回复。"
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return "prompt 为空。请提供想要画的画面描述后重新调用。"
        timeout = max(
            30.0, float(getattr(config, "minimax_timeout_seconds", 120.0) or 120.0)
        )
        try:
            data = await asyncio.wait_for(
                service.tool_draw(
                    conversation_id=conversation_id,
                    prompt=prompt,
                    aspect_ratio=str(arguments.get("aspect_ratio") or ""),
                    style=str(arguments.get("style") or ""),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return (
                "绘图工具执行超时（生成仍在排队或进行中）。不要编造结果，"
                "可建议用户稍后再试。"
            )
        except Exception as exc:
            return f"绘图工具执行失败：{exc}。不要编造结果。"
        return _json.dumps(data, ensure_ascii=False)

    async def _execute_anima_tool(
        self,
        conversation_id: str,
        name: str,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import asyncio
        import json as _json

        from ..anima import get_anima_service

        if not bool(getattr(config, "anima_enabled", False)):
            return "绘画助手未启用，请直接用文字自然回复。"
        service = get_anima_service(config)
        if service is None:
            return "绘画助手未启用，请直接用文字自然回复。"

        timeout = max(
            60.0, float(getattr(config, "anima_tool_timeout_seconds", 420.0) or 420.0)
        )
        try:
            if name == "anima_draw":
                prompt = str(arguments.get("prompt") or "").strip()
                if not prompt:
                    return "prompt 为空。请提供想要画的画面描述后重新调用。"

                def _optional_int(key: str) -> int | None:
                    value = arguments.get(key)
                    if value is None:
                        return None
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return None

                data = await asyncio.wait_for(
                    service.tool_generate(
                        conversation_id,
                        prompt,
                        width=_optional_int("width"),
                        height=_optional_int("height"),
                    ),
                    timeout=timeout,
                )
            elif name == "anima_status":
                text = await asyncio.wait_for(service.tool_status(), timeout=30.0)
                return str(text or "绘图服务状态未知。")
            else:
                return f"未知绘画工具：{name}。不要编造结果。"
        except asyncio.TimeoutError:
            return (
                "绘图工具执行超时（可能仍在排队或生成中）。不要编造结果，"
                "可建议用户稍后再试或用 anima_status 检查服务状态。"
            )
        except Exception as exc:
            return f"绘图工具执行失败：{exc}。不要编造结果。"
        return _json.dumps(data, ensure_ascii=False)

    async def _execute_bilibili_read_tool(
        self,
        conversation_id: str,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import asyncio
        import json as _json

        from ..bilibili import get_bilibili_service

        if not bool(getattr(config, "bilibili_enabled", True)):
            return "B站助手未启用，请直接用文字自然回复。"
        service = get_bilibili_service(config)

        timeout = max(
            10.0,
            float(getattr(config, "bilibili_ai_tool_timeout_seconds", 150.0) or 150.0),
        )
        try:
            data = await asyncio.wait_for(
                service.tool_read_video(conversation_id, str(arguments.get("video") or "")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return (
                "B站视频读取超时（可能正在转写音频）。不要编造视频内容，"
                "可建议用户稍后再试或使用「B站总结」命令。"
            )
        except Exception as exc:
            return f"B站工具执行失败：{exc}。不要编造结果。"
        return _json.dumps(data, ensure_ascii=False)

    async def _execute_manga_tool(
        self,
        conversation_id: str,
        name: str,
        arguments: dict[str, Any],
        config: Any,
    ) -> str:
        import asyncio
        import json as _json

        from ..manga import MangaDeliveryRequest, SuwayomiError, get_manga_service

        service = get_manga_service(config)
        if service is None:
            return "漫画助手未启用，请直接用文字自然回复。"

        timeout = max(
            5.0, float(getattr(config, "manga_ai_tool_timeout_seconds", 60.0) or 60.0)
        )
        try:
            if name == "suwayomi_search_manga":
                data = await asyncio.wait_for(
                    service.tool_search_manga(
                        str(arguments.get("query") or ""),
                        source_hint=str(arguments.get("source_hint") or ""),
                        search_all_sources=bool(arguments.get("search_all_sources", False)),
                    ),
                    timeout=timeout,
                )
            elif name == "suwayomi_get_chapters":
                try:
                    limit = int(arguments.get("limit") or 20)
                except (TypeError, ValueError):
                    limit = 20
                data = await asyncio.wait_for(
                    service.tool_get_chapters(
                        conversation_id,
                        arguments.get("manga_id"),
                        selector=str(arguments.get("selector") or "latest"),
                        refresh=bool(arguments.get("refresh", False)),
                        limit=limit,
                    ),
                    timeout=timeout,
                )
            elif name == "suwayomi_get_subscriptions":
                data = await asyncio.wait_for(
                    service.tool_get_subscriptions(conversation_id),
                    timeout=timeout,
                )
            elif name == "suwayomi_subscribe":
                push_enabled = arguments.get("push_enabled")
                data = await asyncio.wait_for(
                    service.tool_subscribe(
                        conversation_id,
                        arguments.get("manga_id"),
                        push_enabled=(
                            None
                            if push_enabled is None
                            else bool(push_enabled)
                        ),
                    ),
                    timeout=timeout,
                )
            elif name == "suwayomi_unsubscribe":
                data = await asyncio.wait_for(
                    service.tool_unsubscribe(conversation_id, arguments.get("manga_id")),
                    timeout=timeout,
                )
            elif name == "suwayomi_send_chapter":
                return await self._request_manga_delivery(
                    conversation_id, arguments, config, service, timeout
                )
            else:
                return f"未知漫画工具：{name}。不要编造结果。"
        except asyncio.TimeoutError:
            return "漫画工具执行超时。不要编造结果，可建议用户稍后再试。"
        except SuwayomiError as exc:
            return f"漫画服务暂时不可用：{exc}。不要编造结果。"
        except Exception as exc:
            return f"漫画工具执行失败：{exc}。不要编造结果。"
        return _json.dumps(data, ensure_ascii=False)

    async def _request_manga_delivery(
        self,
        conversation_id: str,
        arguments: dict[str, Any],
        config: Any,
        service: Any,
        timeout: float,
    ) -> str:
        import asyncio

        from ..manga import MangaDeliveryRequest

        if not bool(getattr(config, "manga_allow_ai_send", True)):
            return (
                "当前不允许主动发送章节，请引导用户使用「漫画 阅读」命令。"
            )
        try:
            manga_id = int(arguments.get("manga_id"))
            chapter_id = int(arguments.get("chapter_id"))
        except (TypeError, ValueError):
            return "manga_id 和 chapter_id 必须是整数。"
        fmt = str(arguments.get("fmt") or "").strip().lower()
        if fmt not in {"image", "pdf", "zip", "cbz"}:
            fmt = "image"

        accepted, note = await asyncio.wait_for(
            service.tool_send_chapter(conversation_id, manga_id, chapter_id),
            timeout=timeout,
        )
        if not accepted:
            return f"章节发送未受理：{note}"
        self.register_manga_delivery(
            conversation_id,
            MangaDeliveryRequest(manga_id=manga_id, chapter_id=chapter_id, fmt=fmt),
        )
        return (
            f"章节「{note}」已受理，系统会自动下载并发送。"
            "最终回复只需用一句自然的话告知正在送达，不要描述下载或打包过程，"
            "也不要声称已经发完。"
        )

    async def _review_web_grounding(
        self,
        client: Any,
        headers: dict[str, str],
        user_text: str,
        candidate_reply: str,
        receipts: list[ToolExecutionReceipt],
    ) -> str:
        evidence_parts: list[str] = []
        remaining = 10_000
        for receipt in receipts:
            if not receipt.ok or receipt.name not in {"search_web", "open_web_page"}:
                continue
            excerpt = receipt.content[: min(4_000, remaining)]
            if excerpt:
                evidence_parts.append(f"[{receipt.name}]\n{excerpt}")
                remaining -= len(excerpt)
            if remaining <= 0:
                break
        if not evidence_parts:
            return candidate_reply

        review_messages = [
            {
                "role": "system",
                "content": (
                    "你是严格的联网证据审校器。只依据下面工具实际返回的搜索结果和网页正文重写候选回复。"
                    "删除证据中没有出现的精确数字、版本、论文名、性能结论、因果关系和所谓官方说法；"
                    "不同来源相互冲突、只有单一来源或研究仍在推进时，要明确写尚无定论或证据有限。"
                    "保留问题背景、已有研究方向和仍未解决的部分，但不得用常识补造新事实。"
                    "不要提内部审校过程，不要承诺稍后继续搜索，只输出可直接发送给用户的中文回复。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{user_text}\n\n"
                    "本轮工具证据：\n"
                    + "\n\n".join(evidence_parts)
                    + f"\n\n候选回复：\n{candidate_reply}"
                ),
            },
        ]
        review_payload = {
            "model": self.config.openai_model,
            "messages": review_messages,
            "temperature": 0.1,
            "max_tokens": max(900, int(self.config.max_tokens)),
        }
        review_payload.update(
            _provider_payload_overrides(
                self.config.openai_base_url,
                self.config.openai_model,
            )
        )
        try:
            data = await self._post_chat_completion(client, headers, review_payload)
            reviewed = _normalize_reply(
                str(data["choices"][0]["message"].get("content") or "")
            ).strip()
        except Exception:
            return candidate_reply
        if not reviewed or has_illegal_language_or_garbage(reviewed):
            return candidate_reply
        if has_unsupported_deferred_action(reviewed):
            return candidate_reply
        return reviewed

    async def _review_history_grounding(
        self,
        user_text: str,
        candidate_reply: str,
        recent_messages: list[dict[str, str]],
    ) -> str:
        import httpx

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是对话事实审校器。只检查候选回复是否把 assistant 的旧话、"
                    "推测或不存在的细节说成用户过去做过或说过的事实。"
                    "role=user 才是用户原话，role=assistant 是模型旧回复。"
                    "不得新增任何历史例子。用户在质疑无依据的猜测时，"
                    "应由模型承认自己刚才乱猜，不要反过来要求用户提供证据。"
                    "保持亲近自然的聊天口吻，禁止出现“证据、事实审校、role、候选回复”等内部词，"
                    "不解释审校过程。"
                ),
            },
            *recent_messages,
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": candidate_reply},
            {
                "role": "user",
                "content": (
                    "输出一个 JSON 对象，格式为 "
                    '{"reply":"最终回复"}。'
                    "事实有误就修正；事实有明确 user 消息依据就保留。"
                ),
            },
        ]
        payload: dict[str, Any] = {
            "model": self.config.openai_model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 900,
        }
        payload.update(
            _provider_payload_overrides(
                self.config.openai_base_url,
                self.config.openai_model,
            )
        )
        if (
            "api.deepseek.com" in str(self.config.openai_base_url).casefold()
            and str(self.config.openai_model).strip().casefold()
            in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"}
        ):
            payload["thinking"] = {"type": "enabled"}
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
        async with httpx.AsyncClient(
            timeout=_chat_http_timeout(self.config)
        ) as client:
            data = await self._post_chat_completion(client, headers, payload)
        raw = str(data["choices"][0]["message"].get("content") or "").strip()
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return ""
        try:
            parsed = json.loads(match.group(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        reply = _normalize_reply(str(parsed.get("reply") or "")).strip()
        if not reply or has_illegal_language_or_garbage(reply):
            return ""
        return reply.strip()

    async def _post_chat_completion(
        self,
        client: Any,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        import httpx

        async def post_once(request_payload: dict[str, Any]) -> Any:
            return await client.post(
                f"{self.config.openai_base_url}/chat/completions",
                headers=headers,
                json=request_payload,
            )

        async def post_local(request_payload: dict[str, Any]) -> Any:
            async with inference_resource_lease(
                f"chat:{self.config.openai_model}",
                timeout_seconds=float(
                    getattr(self.config, "chat_resource_wait_seconds", 35.0)
                ),
                priority=InferencePriority.CHAT,
            ):
                if not await ensure_ollama_service(timeout_seconds=15.0):
                    raise RuntimeError("本地聊天服务暂时不可用")
                try:
                    return await post_once(request_payload)
                finally:
                    await release_ollama_model(
                        self.config.openai_model,
                        self.config.openai_base_url,
                    )

        try:
            if _is_local_ollama_endpoint(self.config.openai_base_url):
                response = await post_local(payload)
            else:
                response = await post_once(payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if "tools" not in payload or not _looks_like_tool_schema_error(exc):
                raise
            LOGGER.warning(
                "当前模型端点拒绝了工具 schema；显式语音请求仍受保护，自主工具暂不可用。"
            )
            retry_payload = dict(payload)
            retry_payload.pop("tools", None)
            retry_payload.pop("tool_choice", None)
            retry_messages = list(retry_payload.get("messages") or [])
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "当前模型接口拒绝了工具协议，因此本轮没有执行任何搜索、网页读取、天气或时间工具。"
                        "不得声称已经搜索、正在搜索或获得了最新资料。涉及实时、前沿或无法确认的问题，"
                        "只能诚实说明当前没有取得可靠外部信息；普通闲聊可以正常回答。"
                    ),
                }
            )
            retry_payload["messages"] = retry_messages
            if _is_local_ollama_endpoint(self.config.openai_base_url):
                response = await post_local(retry_payload)
            else:
                response = await post_once(retry_payload)
            response.raise_for_status()
        return response.json()

    async def _reply_with_guarded_api(
        self,
        conversation_id: str,
        user_text: str,
        nickname: str | None,
        extra_system: str | None = None,
        profile_id: str | None = None,
        profile: dict[str, Any] | None = None,
        context_profile: dict[str, Any] | None = None,
        iteration_decision: Any | None = None,
        tool_context: Any | None = None,
        reply_focus: GroupReplyFocus | None = None,
    ) -> str:
        last_reply = ""
        visual_temperature = (
            min(0.25, float(getattr(self.config, "temperature", 0.6)))
            if _has_current_visual_context(tool_context)
            else None
        )
        retry_temperatures = (
            visual_temperature,
            min(0.45, float(getattr(self.config, "temperature", 0.6))),
            0.30,
        )[: 1 + max(0, min(2, int(getattr(self.config, "chat_quality_retries", 1))))]
        for attempt, temperature in enumerate(retry_temperatures):
            retry_system = extra_system
            if attempt:
                parts = [extra_system, LANGUAGE_RETRY_PROMPT]
                if last_reply:
                    parts.append(f"不合格回复：{_shorten(last_reply, 160)}")
                retry_system = "\n\n".join(part for part in parts if part)
            reply = await self._reply_with_api(
                conversation_id,
                user_text,
                nickname,
                extra_system=retry_system,
                profile_id=profile_id,
                profile=profile,
                context_profile=context_profile,
                iteration_decision=iteration_decision,
                tool_context=tool_context,
                reply_focus=reply_focus,
                temperature_override=temperature,
                frequency_penalty_override=(
                    None
                    if attempt == 0
                    else min(
                        1.0,
                        float(getattr(self.config, "frequency_penalty", 0.25))
                        + 0.15 * attempt,
                    )
                ),
            )
            last_reply = reply
            if reply and not has_illegal_language_or_garbage(reply):
                return reply
        raise RuntimeError("模型连续 3 次返回空内容、乱码或异常语言")


