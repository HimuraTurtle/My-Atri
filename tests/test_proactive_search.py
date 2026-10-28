"""主动联网检索测试：门槛过滤、检索词构造、证据注入与轮内备忘。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from atri_qq_bot.llm_tools import proactive_search as proactive_module
from atri_qq_bot.llm_tools.proactive_search import (
    build_proactive_query,
    proactive_search_evidence,
    proactive_search_gate,
)
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(**overrides):
    base = {
        "web_search_enabled": True,
        "web_search_proactive_enabled": True,
        "web_search_max_results": 5,
        "web_search_timeout_seconds": 6.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestGate:
    def test_disabled_web_search_skips(self):
        assert proactive_search_gate("最新版本发布了", _config(web_search_enabled=False)) is False

    def test_disabled_proactive_skips(self):
        assert (
            proactive_search_gate("最新版本发布了", _config(web_search_proactive_enabled=False))
            is False
        )

    def test_short_or_empty_messages_skip(self):
        assert proactive_search_gate("", _config()) is False
        assert proactive_search_gate("嗯", _config()) is False
        assert proactive_search_gate("你好呀", _config()) is False

    def test_trivial_greetings_skip(self):
        for text in ("早上好", "晚安", "在吗？", "好的。", "拜拜～", "hello!", "ok"):
            assert proactive_search_gate(text, _config()) is False, text

    def test_repeated_emotes_skip(self):
        for text in ("哈哈哈哈", "嘿嘿嘿", "呜呜呜呜", "哈哈哈哈哈！"):
            assert proactive_search_gate(text, _config()) is False, text

    def test_substantive_messages_pass(self):
        for text in (
            "你知道樱羽艾玛吗",
            "GPT-5 什么时候发布",
            "明天上海会下雨吗",
            "最近有什么新番推荐",
        ):
            assert proactive_search_gate(text, _config()) is True, text


class TestBuildQuery:
    def test_strips_conversational_fillers(self):
        assert build_proactive_query("你知道樱羽艾玛吗") == "樱羽艾玛"
        assert build_proactive_query("帮我查一下今天有什么新闻") == "今天新闻"

    def test_strips_question_words(self):
        assert build_proactive_query("查一下今年第五人格亚运会冠军是谁，队员有谁") == (
            "今年第五人格亚运会冠军，队员"
        )
        assert build_proactive_query("最近有哪些新番") == "最近新番"
        assert build_proactive_query("姆巴佩近况怎么样") == "姆巴佩近况"

    def test_strips_tail_particles(self):
        assert build_proactive_query("GPT-5 什么时候发布呢？") == "GPT-5 什么时候发布"

    def test_falls_back_to_original_when_all_filler(self):
        assert build_proactive_query("你知道吗") == "你知道吗"

    def test_truncates_long_messages(self):
        query = build_proactive_query("今天" + "天气很好适合出去玩" * 10)
        assert len(query) <= 40

    def test_empty_text_falls_back_to_empty(self):
        assert build_proactive_query("") == ""


class TestEvidence:
    def test_gate_failure_returns_none_without_search(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("不应发起搜索")

        monkeypatch.setattr(proactive_module, "search_web", boom)
        assert asyncio.run(proactive_search_evidence("晚安", _config())) is None

    def test_search_results_wrapped_with_header(self, monkeypatch):
        async def fake_search(arguments, config):
            return f"搜索时间：2026-10-28 12:00:00\n搜索关键词：{arguments['query']}\n1. 标题：测试结果"

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        evidence = asyncio.run(proactive_search_evidence("你知道樱羽艾玛吗", _config()))
        assert evidence is not None
        assert evidence.startswith("联网检索（系统已主动执行）")
        assert "樱羽艾玛" in evidence
        assert "测试结果" in evidence

    def test_search_failure_note_is_injected_not_raised(self, monkeypatch):
        async def fake_search(arguments, config):
            return "搜索失败：网络不可用。不要编造实时信息。"

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        evidence = asyncio.run(proactive_search_evidence("最近有什么新闻", _config()))
        assert evidence is not None
        assert "搜索失败" in evidence
        assert evidence.startswith("联网检索（系统已主动执行）")

    def test_search_receives_cleaned_query_and_capped_results(self, monkeypatch):
        captured: list[dict] = []

        async def fake_search(arguments, config):
            captured.append(dict(arguments))
            return "1. 标题：结果"

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        asyncio.run(proactive_search_evidence("你知道樱羽艾玛吗", _config()))
        assert captured == [
            {"query": "樱羽艾玛", "mode": "auto", "max_results": 3}
        ]

    def test_time_sensitive_message_uses_news_mode(self, monkeypatch):
        captured: list[dict] = []

        async def fake_search(arguments, config):
            captured.append(dict(arguments))
            return "1. 标题：结果"

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        for text in (
            "最近有什么时事新闻",
            "今天发生了什么大事",
            "姆巴佩近况怎么样",
            "查一下今年第五人格亚运会冠军是谁，队员有谁",
        ):
            captured.clear()
            asyncio.run(proactive_search_evidence(text, _config()))
            assert captured[0]["mode"] == "news", text

    def test_plain_question_uses_auto_mode(self, monkeypatch):
        captured: list[dict] = []

        async def fake_search(arguments, config):
            captured.append(dict(arguments))
            return "1. 标题：结果"

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        for text in (
            "你知道樱羽艾玛吗",
            "GPT-5 什么时候发布",
            "量子计算机的原理是什么",
        ):
            captured.clear()
            asyncio.run(proactive_search_evidence(text, _config()))
            assert captured[0]["mode"] == "auto", text

    def test_unexpected_search_exception_propagates_to_caller(self, monkeypatch):
        async def fake_search(arguments, config):
            raise RuntimeError("boom")

        monkeypatch.setattr(proactive_module, "search_web", fake_search)
        try:
            asyncio.run(proactive_search_evidence("最近有什么新闻", _config()))
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("异常应向上传播，由引擎层兜底")


class _FakeEngine(EngineToolsMixin):
    def __init__(self, config):
        self.config = config
        self._proactive_search_memo = {}


class TestEngineMemo:
    # 引擎混入模块的符号由 core.py 的 _sync_core_globals 动态同步，
    # 因此桩必须打在同步源头 core 模块上。
    def test_same_turn_reuses_evidence_without_research(self, monkeypatch):
        calls: list[str] = []

        async def fake_evidence(user_text, config):
            calls.append(user_text)
            return "联网检索（系统已主动执行）：证据A"

        monkeypatch.setattr(
            "atri_qq_bot.persona.core.proactive_search_evidence", fake_evidence
        )
        engine = _FakeEngine(_config())
        first = asyncio.run(engine._proactive_search_for_turn("c1", "今天有什么新闻"))
        second = asyncio.run(engine._proactive_search_for_turn("c1", "今天有什么新闻"))
        assert first == second == "联网检索（系统已主动执行）：证据A"
        assert calls == ["今天有什么新闻"]

    def test_new_message_triggers_new_search(self, monkeypatch):
        async def fake_evidence(user_text, config):
            return f"证据：{user_text}"

        monkeypatch.setattr(
            "atri_qq_bot.persona.core.proactive_search_evidence", fake_evidence
        )
        engine = _FakeEngine(_config())
        first = asyncio.run(engine._proactive_search_for_turn("c1", "第一条消息"))
        second = asyncio.run(engine._proactive_search_for_turn("c1", "第二条消息"))
        assert first == "证据：第一条消息"
        assert second == "证据：第二条消息"

    def test_gate_skip_is_also_memoized(self, monkeypatch):
        calls: list[str] = []

        async def fake_evidence(user_text, config):
            calls.append(user_text)
            return None

        monkeypatch.setattr(
            "atri_qq_bot.persona.core.proactive_search_evidence", fake_evidence
        )
        engine = _FakeEngine(_config())
        assert asyncio.run(engine._proactive_search_for_turn("c1", "晚安")) is None
        assert asyncio.run(engine._proactive_search_for_turn("c1", "晚安")) is None
        assert calls == ["晚安"]

    def test_search_exception_degrades_to_none(self, monkeypatch):
        async def fake_evidence(user_text, config):
            raise RuntimeError("网络错误")

        monkeypatch.setattr(
            "atri_qq_bot.persona.core.proactive_search_evidence", fake_evidence
        )
        engine = _FakeEngine(_config())
        assert asyncio.run(engine._proactive_search_for_turn("c1", "今天有什么新闻")) is None
