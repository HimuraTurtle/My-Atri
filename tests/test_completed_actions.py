"""任务账本 + 自洽审查（方案一/二/三）测试。

覆盖：账本读写与窗口过滤、矛盾判定规则（实锤案例、双向话题提取、
同族动词、漏拦放行方向）、账本上下文渲染、工具回执摘要映射、
引擎记账与诊断快照、reply() 轮次重置与矛盾重写挂载。全部离线。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.persona import AtriReplyEngine
from atri_qq_bot.persona.reply_rules import (
    _completed_actions_prompt,
    _completed_task_contradiction,
)
from atri_qq_bot.persona.engine_tools import _tool_action_summary


def _config(tmp_path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
        openai_model="test-model",
        temperature=0.4,
        max_tokens=350,
        memory_path=tmp_path / "memory.json",
        llm_tools_enabled=False,
    )


class TestLedgerStore:
    def test_record_and_recent_roundtrip(self, tmp_path) -> None:
        store = UserMemoryStore(tmp_path / "memory.json")
        now = time.time()
        store.record_completed_action("private:1", "搜索了「瑞士轮」", now=now)
        store.record_completed_action(
            "private:1", "总结了B站视频 BV1xx", action="bilibili_summary", now=now
        )
        entries = store.recent_completed_actions("private:1", now=now)
        assert [item["text"] for item in entries] == [
            "总结了B站视频 BV1xx",
            "搜索了「瑞士轮」",
        ]
        assert entries[0]["action"] == "bilibili_summary"

    def test_window_filter_drops_stale_entries(self, tmp_path) -> None:
        store = UserMemoryStore(tmp_path / "memory.json")
        now = time.time()
        store.record_completed_action("private:1", "旧动作", now=now - 3600.0)
        store.record_completed_action("private:1", "新动作", now=now)
        assert store.recent_completed_actions("private:1", now=now) == [
            {"text": "新动作", "action": "", "at": pytest.approx(now)}
        ]

    def test_keeps_only_last_twelve(self, tmp_path) -> None:
        store = UserMemoryStore(tmp_path / "memory.json")
        now = time.time()
        for index in range(15):
            store.record_completed_action("private:1", f"动作{index}", now=now - index)
        item = store._conversation("private:1", save=False)
        assert len(item["completed_actions"]) == 12
        assert item["completed_actions"][0]["text"] == "动作3"

    def test_empty_summary_ignored(self, tmp_path) -> None:
        store = UserMemoryStore(tmp_path / "memory.json")
        store.record_completed_action("private:1", "   ")
        store.record_completed_action("private:1", "")
        assert store.recent_completed_actions("private:1") == []

    def test_limit_respected(self, tmp_path) -> None:
        store = UserMemoryStore(tmp_path / "memory.json")
        now = time.time()
        for index in range(5):
            store.record_completed_action("private:1", f"动作{index}", now=now - index)
        assert len(store.recent_completed_actions("private:1", limit=2, now=now)) == 2


class TestContradictionRule:
    def _ledger(self, text: str, age_seconds: float = 60.0) -> dict[str, Any]:
        return {"text": text, "action": "", "at": time.time() - age_seconds}

    def test_hits_confirmed_case_with_prefixed_topic(self) -> None:
        ledger = [self._ledger("搜索了「亚洲最年轻的将军」")]
        facts = _completed_task_contradiction(
            "亚洲最年轻的将军我还没查呢", ledger
        )
        assert "搜索了「亚洲最年轻的将军」" in facts

    def test_hits_deferred_claim_with_prefixed_topic(self) -> None:
        ledger = [self._ledger("搜索了「瑞士轮对阵」")]
        assert _completed_task_contradiction("瑞士轮的对阵我这就去搜", ledger)

    def test_hits_deferred_claim_with_suffixed_topic(self) -> None:
        ledger = [self._ledger("搜索了「瑞士轮对阵」")]
        assert _completed_task_contradiction("这就去搜瑞士轮的对阵", ledger)

    def test_hits_same_verb_family_within_five_minutes(self) -> None:
        ledger = [self._ledger("搜索了「别的什么」", age_seconds=60.0)]
        assert _completed_task_contradiction("我还没查呢", ledger)

    def test_misses_when_recent_verb_action_is_old(self) -> None:
        ledger = [self._ledger("搜索了「别的什么」", age_seconds=600.0)]
        assert _completed_task_contradiction("我还没查呢", ledger) == ""

    def test_misses_life_sentence_without_task_verb(self) -> None:
        ledger = [self._ledger("搜索了「亚洲最年轻的将军」")]
        assert _completed_task_contradiction("还没吃饭呢，好饿", ledger) == ""

    def test_misses_unrelated_topic(self) -> None:
        ledger = [self._ledger("搜索了「亚洲最年轻的将军」")]
        assert _completed_task_contradiction("还没查天气呢", ledger) == ""

    def test_misses_empty_ledger(self) -> None:
        assert (
            _completed_task_contradiction("亚洲最年轻的将军我还没查呢", []) == ""
        )

    def test_honest_todo_without_ledger_evidence_passes(self) -> None:
        ledger = [self._ledger("解析了B站视频 BV1abc")]
        assert _completed_task_contradiction("还没查到瑞士轮的赛程呢", ledger) == ""


class TestCompletedActionsPrompt:
    def test_renders_entries_with_relative_time(self) -> None:
        now = time.time()
        actions = [
            {"text": "搜索了「瑞士轮」", "action": "search_web", "at": now - 90.0},
            {"text": "查看了当前时间", "action": "get_current_time", "at": now - 5.0},
        ]
        prompt = _completed_actions_prompt(actions, now=now)
        assert "已经真实完成" in prompt
        assert "- [1分钟前]搜索了「瑞士轮」" in prompt
        assert "- 查看了当前时间" in prompt

    def test_empty_ledger_renders_nothing(self) -> None:
        assert _completed_actions_prompt([]) == ""
        assert _completed_actions_prompt([{"text": "", "action": "", "at": 0.0}]) == ""


class TestToolActionSummary:
    def test_search_web_with_query(self) -> None:
        assert (
            _tool_action_summary("search_web", {"query": "瑞士轮对阵"})
            == "搜索了「瑞士轮对阵」"
        )

    def test_search_web_without_query(self) -> None:
        assert _tool_action_summary("search_web", {}) == "搜索了网页资料"

    def test_send_chapter_keeps_delivery_wording(self) -> None:
        assert (
            _tool_action_summary("suwayomi_send_chapter", {})
            == "受理了漫画章节发送（系统会自动下载送达）"
        )

    def test_bilibili_read(self) -> None:
        assert (
            _tool_action_summary("bilibili_read", {"video": "BV1abc"})
            == "解析了B站视频 BV1abc"
        )

    def test_unknown_tool_falls_back(self) -> None:
        assert (
            _tool_action_summary("mystery_tool", {"x": 1})
            == "完成了 mystery_tool 工具调用"
        )


class TestEngineLedgerWiring:
    def test_record_completed_actions_writes_ledger_and_diagnostics(
        self, tmp_path
    ) -> None:
        engine = AtriReplyEngine(_config(tmp_path))
        receipts = [
            SimpleNamespace(
                name="search_web",
                arguments={"query": "瑞士轮对阵"},
                ok=True,
                elapsed_ms=120,
            ),
            SimpleNamespace(
                name="search_web",
                arguments={"query": "失败查询"},
                ok=False,
                elapsed_ms=10,
            ),
        ]
        engine._record_completed_actions("private:1", receipts)
        entries = engine.memory.recent_completed_actions("private:1")
        assert [item["text"] for item in entries] == ["搜索了「瑞士轮对阵」"]
        snapshot = engine.turn_diagnostics_snapshot("private:1")
        assert snapshot["tool_calls"] == [
            {"name": "search_web", "ok": True, "elapsed_ms": 120}
        ]
        assert snapshot["ledger_snapshot"][0]["text"] == "搜索了「瑞士轮对阵」"

    def test_engine_passthrough_records_action(self, tmp_path) -> None:
        engine = AtriReplyEngine(_config(tmp_path))
        engine.record_completed_action(
            "group:2", "总结了B站视频 BV1xx", action="bilibili_summary"
        )
        entries = engine.memory.recent_completed_actions("group:2")
        assert entries[0]["text"] == "总结了B站视频 BV1xx"

    def test_reply_resets_turn_diagnostics(self, tmp_path) -> None:
        disabled = BotConfig(
            bot_qq=100000001,
            host="127.0.0.1",
            port=8765,
            reply_mode="smart",
            openai_api_key=None,
            openai_base_url="https://example.test/v1",
            openai_model="test-model",
            temperature=0.4,
            max_tokens=350,
            memory_path=tmp_path / "memory.json",
            llm_tools_enabled=False,
        )
        engine = AtriReplyEngine(disabled)
        engine.note_turn_tool_call("private:1", "search_web", True, 50)
        assert engine.turn_diagnostics_snapshot("private:1")["tool_calls"]
        asyncio.run(engine.reply("private:1", "在吗"))
        assert engine.turn_diagnostics_snapshot("private:1")["tool_calls"] == []


class TestContradictionRewriteMount:
    def test_reply_rewrites_todo_claim_using_ledger_facts(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = AtriReplyEngine(_config(tmp_path))
        engine.record_completed_action("private:1", "搜索了「亚洲最年轻的将军」")
        calls: list[str | None] = []

        async def fake_guarded(
            conversation_id: str,
            user_text: str,
            nickname: str | None,
            extra_system: str | None = None,
            **kwargs: Any,
        ) -> str:
            calls.append(extra_system)
            if len(calls) == 1:
                return "亚洲最年轻的将军我还没查呢"
            return "查过啦，亚洲最年轻的将军没有公认答案哦。"

        monkeypatch.setattr(engine, "_reply_with_guarded_api", fake_guarded)
        monkeypatch.setattr(
            engine,
            "_finalize_reply",
            lambda conversation_id, user_text, reply, **kwargs: reply,
        )
        reply = asyncio.run(engine.reply("private:1", "将军是谁呀"))
        assert reply == "查过啦，亚洲最年轻的将军没有公认答案哦。"
        assert "已经完成" in (calls[1] or "")
        assert "搜索了「亚洲最年轻的将军」" in (calls[1] or "")
        snapshot = engine.turn_diagnostics_snapshot("private:1")
        assert snapshot["contradiction_checks"][0]["resolved"] is True

    def test_reply_passes_through_when_no_contradiction(
        self, tmp_path, monkeypatch
    ) -> None:
        engine = AtriReplyEngine(_config(tmp_path))
        engine.record_completed_action("private:1", "搜索了「亚洲最年轻的将军」")

        async def fake_guarded(
            conversation_id: str,
            user_text: str,
            nickname: str | None,
            extra_system: str | None = None,
            **kwargs: Any,
        ) -> str:
            assert "已经完成" not in (extra_system or "")
            return "查到啦，亚洲最年轻的将军没有公认答案哦。"

        monkeypatch.setattr(engine, "_reply_with_guarded_api", fake_guarded)
        monkeypatch.setattr(
            engine,
            "_finalize_reply",
            lambda conversation_id, user_text, reply, **kwargs: reply,
        )
        reply = asyncio.run(engine.reply("private:1", "将军是谁呀"))
        assert reply == "查到啦，亚洲最年轻的将军没有公认答案哦。"
        snapshot = engine.turn_diagnostics_snapshot("private:1")
        assert snapshot["contradiction_checks"] == []


class _FakeEngine:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, str]] = []

    def record_completed_action(
        self, conversation_id: str, summary: str, *, action: str = ""
    ) -> None:
        self.recorded.append((conversation_id, summary, action))


class TestDeterministicCommandLedger:
    def test_manga_delivery_command_recorded(self) -> None:
        from atri_qq_bot.onebot.server_parts.server_manga import OneBotMangaMixin

        mixin = object.__new__(OneBotMangaMixin)
        engine = _FakeEngine()
        mixin.reply_engine = engine
        mixin._record_manga_command_action("group:2", "亚托莉，漫画 阅读 海贼王 12")
        assert engine.recorded == [
            ("group:2", "做了漫画「海贼王 12」的阅读", "manga_阅读")
        ]

    def test_manga_help_command_not_recorded(self) -> None:
        from atri_qq_bot.onebot.server_parts.server_manga import OneBotMangaMixin

        mixin = object.__new__(OneBotMangaMixin)
        engine = _FakeEngine()
        mixin.reply_engine = engine
        mixin._record_manga_command_action("group:2", "漫画 帮助")
        assert engine.recorded == []

    def test_bilibili_video_ref_extracts_bv(self) -> None:
        from atri_qq_bot.onebot.server_parts.server_bilibili import (
            OneBotBilibiliMixin,
        )

        assert (
            OneBotBilibiliMixin._bilibili_video_ref("B站总结 BV1abc123def 谢谢")
            == "BV1abc123def"
        )
        assert OneBotBilibiliMixin._bilibili_video_ref("B站总结") == ""
