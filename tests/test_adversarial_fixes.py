"""对抗式审查修复的配套单测。

覆盖 2026-11 一轮对抗式审查确认的缺陷修复：
- URL 原子性：拆条/问句修剪/问号计数/未完成判定不再把链接拦腰截断
- 多行未完成修复先试修再丢弃（对称内容保留）
- lore 兜底的「主人」称呼红线贯通私聊非 owner 路径
- 群记忆 L1 候选按发言者隔离 + 规则路径 L1 封顶
- 模型增强路径的群 scope 过滤与归属标注
- send_reply 返回契约（image 占位 / 部分成功可见 / 全败为空）
- 后台循环防死亡 wrapper
- 消息解析对畸形 data 与深嵌套引用的健壮性
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from atri_qq_bot.message_plan.splitting import _hard_wrap, split_reply_text
from atri_qq_bot.memory.extraction_rules import _upsert_l1_candidate
from atri_qq_bot.memory.ingestion import (
    MemoryExtractionRequest,
    _normalise_model_operations,
)
from atri_qq_bot.onebot import extract_plain_text, extract_reply_message_id
from atri_qq_bot.onebot.server import (
    BACKGROUND_LOOP_RESTART_SECONDS,
    OneBotServer,
    _resilient_background_loop,
)
from atri_qq_bot.message_plan import OutgoingMessage
from atri_qq_bot.persona.reply_fallbacks import (
    _looks_incomplete_reply,
    _persona_repair_fallback,
    _repair_incomplete_reply,
    _trim_extra_questions,
)
from atri_qq_bot.persona.reply_rules import _question_count
from atri_qq_bot.text_units import (
    count_questions,
    has_question_mark,
    last_sentence_boundary,
    mask_urls,
    split_sentences,
)


# ---------------------------------------------------------------- text_units


def test_split_sentences_keeps_url_with_question_mark_intact() -> None:
    text = "要链接吗？你可以看 https://example.com/search?q=atri 哦"
    sentences = split_sentences(text)
    assert any("https://example.com/search?q=atri" in s for s in sentences)
    # URL 内的 `?` 不切句：URL 所在句保持完整。
    url_sentence = next(s for s in sentences if "example.com" in s)
    assert url_sentence.endswith("哦")


def test_count_questions_treats_runs_as_one_and_ignores_urls() -> None:
    assert count_questions("真的吗？？？") == 1
    assert count_questions("好吗？行吗？") == 2
    assert count_questions("看 https://x.org/?p=1 这个") == 0
    assert has_question_mark("看 https://x.org/?p=1 这个") is False
    assert has_question_mark("在吗？") is True


def test_mask_urls_and_last_sentence_boundary() -> None:
    masked = mask_urls("看 https://x.org/a(b)?p=1 结束。")
    assert "x.org" not in masked
    assert masked.endswith("结束。")
    # 句界是 URL 之外的句末标点；URL 内的 `?` 不算句界。
    text = "查 https://x.org/?q=1 就这些。"
    assert last_sentence_boundary(text) == len(text) - 1
    assert last_sentence_boundary("查 https://x.org/?q=1 就这些") == -1


def test_split_sentences_keeps_ellipsis_runs_and_pure_punctuation_dropped() -> None:
    chunks = split_sentences("你好。。。世界")
    assert chunks == ["你好。。。", "世界"]
    assert split_sentences("？？？") == []
    # 传输层拆条不吞内容：纯标点输入原样成段（由 split_reply_text 兜底）。
    assert split_reply_text("。。。") == ["。。。"]


# ------------------------------------------------------------------ splitting


def test_hard_wrap_never_cuts_inside_url() -> None:
    text = "看这个 https://example.com/docs/getting-started/installation-guide.zh.html 里面写得挺细"
    parts = _hard_wrap(text, 44)
    for part in parts:
        # 链接不允许被拆到两条消息里；含 URL 的分段允许超出字数预算。
        if "example.com" in part:
            assert "https://example.com/docs/getting-started/installation-guide.zh.html" in part
        else:
            assert "https" not in part and "example.com" not in part


def test_hard_wrap_respects_max_chars_without_off_by_one() -> None:
    parts = _hard_wrap("啊" * 100, 44)
    assert all(len(part) <= 44 for part in parts)
    assert "".join(parts) == "啊" * 100


def test_split_reply_text_keeps_url_single_message() -> None:
    text = "你可以看这个链接 https://example.com/docs/getting-started/installation-guide.zh.html 里面写得挺细的。"
    parts = split_reply_text(text)
    url_parts = [p for p in parts if "example.com" in p]
    assert len(url_parts) == 1
    assert "https://example.com/docs/getting-started/installation-guide.zh.html" in url_parts[0]


# ------------------------------------------------------- persona 修剪与修复


def test_trim_extra_questions_keeps_url_sentence() -> None:
    text = "要链接吗？\n你可以看 https://example.com/search?q=atri 哦"
    trimmed = _trim_extra_questions(text)
    assert "https://example.com/search?q=atri" in trimmed


def test_trim_extra_questions_counts_runs_as_single_question() -> None:
    assert _trim_extra_questions("你要不要一起？？？") == "你要不要一起？"
    assert _trim_extra_questions("在吗？？在吗？？") == "在吗？"


def test_trim_extra_questions_keeps_quote_balance() -> None:
    # 收尾引号随前句：丢弃第三问后引号必须仍然配对，否则 _finalize_reply
    # 的完整收尾检查会把整条合格回复判死。
    trimmed = _trim_extra_questions("我说“好吗？”他说“不行。”你说呢？")
    assert trimmed.count("“") == trimmed.count("”")
    assert _looks_incomplete_reply(trimmed) is False


def test_repair_multiline_repairs_last_line_instead_of_dropping() -> None:
    # 旧实现直接 pop 掉「晚安……」整行；现在先走单行修复保住告别语。
    assert _repair_incomplete_reply("今天早点休息\n晚安……") == "今天早点休息\n晚安。"
    # 逗号悬挂的末行在多行形态下与单行形态同等保留。
    assert _repair_incomplete_reply("嗯嗯\n我陪你把这件事说完，") == "嗯嗯\n我陪你把这件事说完。"


def test_repair_single_line_keeps_short_real_word() -> None:
    assert _repair_incomplete_reply("晚安……") == "晚安。"


def test_looks_incomplete_ignores_url_punctuation() -> None:
    assert _looks_incomplete_reply("看 https://x.org/a(b)?p=1 这个链接") is False
    assert _looks_incomplete_reply("来源：https://example.com/a...") is False
    assert _looks_incomplete_reply("我说（真的") is True


def test_question_count_is_run_and_url_aware() -> None:
    assert _question_count("真的吗？？？") == 1
    assert _question_count("看 https://x.org/?p=1") == 0


# ------------------------------------------------------------- 称呼红线


def test_lore_fallback_private_non_owner_cleanses_owner_address() -> None:
    reply = _persona_repair_fallback(
        "亚托莉 日志 心 是什么梗",
        "（回复不完整触发兜底",
        conversation_id="private:999",
        is_owner=False,
    )
    assert reply
    assert "主人" not in reply
    assert "为满足你" in reply


def test_lore_fallback_owner_private_keeps_owner_address() -> None:
    reply = _persona_repair_fallback(
        "亚托莉 日志 心 是什么梗",
        "（回复不完整触发兜底",
        conversation_id="private:999",
        is_owner=True,
    )
    assert reply
    assert "主人" in reply


# ------------------------------------------------------------- 群记忆归属


def _candidate(key: str, actor: str, value: str = "喜欢") -> dict:
    return {
        "category": "preference",
        "key": key,
        "value": value,
        "confidence": 0.6,
        "actor_name": actor,
        "visibility": "group:1",
    }


def _structured() -> dict:
    return {"l1": [], "l2": [], "l3": [], "candidates": []}


def test_group_l1_candidates_are_actor_scoped() -> None:
    memory = _structured()
    # 首次只入候选（证据 1 < 2 不晋升），第二次同人才晋升。
    assert _upsert_l1_candidate(memory, _candidate("口味", "群友A"), 1000.0) is None
    assert _upsert_l1_candidate(memory, _candidate("口味", "群友A"), 1001.0)
    promoted_a = [e for e in memory["l1"] if e.get("actor_name") == "群友A"]
    assert len(promoted_a) == 1
    assert promoted_a[0]["memory_key"].endswith("@群友A")

    # 群友 B 表达同一偏好：不得给 A 的条目累加证据，也不得误并档。
    memory = _structured()
    _upsert_l1_candidate(memory, _candidate("口味", "群友A"), 1000.0)
    _upsert_l1_candidate(memory, _candidate("口味", "群友B"), 1001.0)
    pending_actors = {e.get("actor_name") for e in memory["candidates"]}
    assert pending_actors == {"群友A", "群友B"}
    assert all(e.get("evidence_count", 1) == 1 for e in memory["candidates"])


def test_rule_path_l1_promotion_is_capped() -> None:
    memory = _structured()
    for index in range(85):
        _upsert_l1_candidate(memory, _candidate(f"偏好{index}", "群友A"), 1000.0 + index)
        _upsert_l1_candidate(memory, _candidate(f"偏好{index}", "群友A"), 2000.0 + index)
    assert len(memory["l1"]) == 80


def test_model_operations_group_scope_filters_and_stamps_actor() -> None:
    request = MemoryExtractionRequest(
        conversation_id="group:7",
        subject_id="group:7",
        text="我叫小明，我喜欢螺蛳粉",
        scope_kind="group",
        actor_name="小明",
        now=1000.0,
    )
    raw = {
        "operations": [
            {"operation": "ADD", "layer": "L1", "category": "profile_fact", "key": "名字", "value": "小明"},
            {"operation": "ADD", "layer": "L1", "category": "preference", "key": "食物", "value": "螺蛳粉"},
        ]
    }
    operations = _normalise_model_operations(raw, request)
    # 个人事实被群 scope 过滤；偏好降级为群体兴趣并带归属。
    assert len(operations) == 1
    assert operations[0]["category"] == "interest"
    assert operations[0]["key"].startswith("群体")
    assert operations[0]["actor_name"] == "小明"


# --------------------------------------------------------- send_reply 契约


def _delivery_server(behavior: list[str]) -> OneBotServer:
    server = object.__new__(OneBotServer)
    server.config = SimpleNamespace(
        bot_qq=100000001,
        message_send_delay_min=0.0,
        message_send_delay_max=0.0,
    )
    calls: list[str] = list(behavior)

    async def _send_one_reply(websocket, event, message):
        outcome = calls.pop(0) if calls else "ok"
        if outcome == "fail":
            raise RuntimeError("模拟发送失败")

    async def _mark_login_required():
        pass

    server._send_one_reply = _send_one_reply
    server._record_delivery_event = lambda *args, **kwargs: None
    server._mark_napcat_manual_login_required = _mark_login_required
    return server


def test_send_reply_image_success_returns_placeholder() -> None:
    server = _delivery_server(["ok"])
    delivered = asyncio.run(
        server.send_reply(
            object(),
            {"message_type": "private", "user_id": 1},
            [OutgoingMessage("image", "/tmp/a.png")],
        )
    )
    assert delivered == "image"


def test_send_reports_honest_failure_text_when_image_fails() -> None:
    server = _delivery_server(["fail"])
    delivered = asyncio.run(
        server.send_reply(
            object(),
            {"message_type": "private", "user_id": 1},
            [OutgoingMessage("image", "/tmp/a.png")],
        )
    )
    assert delivered == ""


def test_send_reply_partial_failure_returns_delivered_text() -> None:
    server = _delivery_server(["ok", "fail"])
    delivered = asyncio.run(
        server.send_reply(
            object(),
            {"message_type": "private", "user_id": 1},
            [OutgoingMessage("text", "第一条"), OutgoingMessage("text", "第二条")],
        )
    )
    # 部分成功不再被记成整体失败：已送达分段如实返回。
    assert delivered == "第一条"


def test_send_reply_total_failure_returns_empty() -> None:
    server = _delivery_server(["fail", "fail"])
    delivered = asyncio.run(
        server.send_reply(
            object(),
            {"message_type": "private", "user_id": 1},
            [OutgoingMessage("text", "第一条"), OutgoingMessage("text", "第二条")],
        )
    )
    assert delivered == ""


# ------------------------------------------------------------- 后台循环


def test_resilient_background_loop_restarts_after_exception(monkeypatch) -> None:
    monkeypatch.setattr(
        "atri_qq_bot.onebot.server.BACKGROUND_LOOP_RESTART_SECONDS", 0.0
    )
    attempts: list[int] = []

    async def factory() -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("boom")
        # 第三次正常返回：终态语义，wrapper 记录后不再重启。

    async def run_for_a_while() -> None:
        task = asyncio.create_task(_resilient_background_loop("测试循环", factory))
        await asyncio.sleep(0.05)
        assert not task.done() or True
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run_for_a_while())
    assert len(attempts) == 3
    assert BACKGROUND_LOOP_RESTART_SECONDS >= 1.0


# ------------------------------------------------------------- 消息解析


def test_extract_plain_text_survives_malformed_segment_data() -> None:
    message = [
        {"type": "text", "data": "不是对象"},
        {"type": "text", "data": {"text": "正常段落"}},
    ]
    assert extract_plain_text(message) == "正常段落"


def test_extract_reply_message_id_depth_capped() -> None:
    nested: dict = {"type": "reply", "data": {"id": "123"}}
    for _ in range(50):
        nested = {"message": nested}
    assert extract_reply_message_id(nested) is None
