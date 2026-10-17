"""事实核查（是真的吗）服务测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from atri_qq_bot.isittrue import get_isittrue_service
from atri_qq_bot.isittrue.service import (
    IsItTrueService,
    _parse_json,
    _parse_verdict,
)


def _config(**overrides):
    base = {
        "isittrue_enabled": True,
        "openai_model": "test-model",
        "openai_base_url": "https://api.test",
        "openai_api_key": "sk-test",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestServiceFactory:
    def test_disabled_returns_none(self):
        assert get_isittrue_service(_config(isittrue_enabled=False)) is None

    def test_enabled_returns_singleton(self):
        svc = get_isittrue_service(_config())
        assert isinstance(svc, IsItTrueService)
        assert get_isittrue_service(_config()) is svc


class TestParseVerdict:
    def test_true_output(self):
        verdict, explanation = _parse_verdict("true\n这条属实，有官方公告支持。")
        assert verdict == "true"
        assert explanation == "这条属实，有官方公告支持。"

    def test_false_output(self):
        verdict, _ = _parse_verdict("false\n谣言，没有任何官方来源。")
        assert verdict == "false"

    def test_unknown_output(self):
        verdict, _ = _parse_verdict("unknown\n纯口味问题，无法核查。")
        assert verdict == "unknown"

    def test_chinese_verdict_fallback(self):
        verdict, _ = _parse_verdict("这是假的\n纯属谣言。")
        assert verdict == "false"

    def test_empty_returns_unknown(self):
        verdict, explanation = _parse_verdict("")
        assert verdict == "unknown"
        assert explanation

    def test_first_line_with_extra_text(self):
        verdict, _ = _parse_verdict("结论：true\n解释内容。")
        assert verdict == "true"


class TestParseJson:
    def test_plain_json(self):
        assert _parse_json('{"verdict": "needs_search"}') == {"verdict": "needs_search"}

    def test_fenced_json(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_prefix(self):
        assert _parse_json('结果：{"a": 1}') == {"a": 1}

    def test_no_json_returns_none(self):
        assert _parse_json("没有 JSON 内容") is None


class TestFactCheckFlow:
    def test_empty_claim(self):
        svc = IsItTrueService(_config())
        result = asyncio.run(svc.fact_check("  "))
        assert result["ok"] is False

    def test_direct_verdict_no_search(self):
        """规划直接给 true 时不触发搜索。"""
        svc = IsItTrueService(_config())

        async def fake_plan(claim):
            return {"verdict": "true", "search_queries": []}

        async def fail_search(*args, **kwargs):
            raise AssertionError("不应触发搜索")

        with (
            patch.object(svc, "_plan", fake_plan),
            patch.object(svc, "_judge", AsyncMock(return_value=("true", "稳定常识。"))),
            patch("atri_qq_bot.isittrue.service.search_web", fail_search),
        ):
            result = asyncio.run(svc.fact_check("地球是圆的"))
        assert result["ok"] is True
        assert result["verdict"] == "true"
        assert result["label"] == "✅ 真的"
        assert result["searched"] is False

    def test_search_path_collects_evidence(self):
        """needs_search 时逐条搜索并写入裁决材料。"""
        svc = IsItTrueService(_config())
        judge_calls = []

        async def fake_plan(claim):
            return {"verdict": "needs_search", "search_queries": ["某游戏 停服 公告"]}

        async def fake_search(arguments, config):
            return f"搜索结果：{arguments.get('query')}"

        async def fake_judge(claim, evidence):
            judge_calls.append(evidence)
            return ("false", "官方未发布停服公告，属谣言。")

        with (
            patch.object(svc, "_plan", fake_plan),
            patch("atri_qq_bot.isittrue.service.search_web", fake_search),
            patch.object(svc, "_judge", fake_judge),
        ):
            result = asyncio.run(svc.fact_check("某游戏下月停服是真的吗"))
        assert result["verdict"] == "false"
        assert result["searched"] is True
        assert "搜索结果：某游戏 停服 公告" in judge_calls[0]

    def test_search_failure_degrades_gracefully(self):
        """搜索全部失败时不阻断，按无资料裁决。"""
        svc = IsItTrueService(_config())

        async def fake_plan(claim):
            return {"verdict": "needs_search", "search_queries": ["查询"]}

        async def fail_search(arguments, config):
            raise RuntimeError("网络错误")

        async def fake_judge(claim, evidence):
            assert evidence == ""  # 搜索全失败返回空，降级文案由 _judge 拼接
            return ("unknown", "没有可靠资料，无法核实。")

        with (
            patch.object(svc, "_plan", fake_plan),
            patch("atri_qq_bot.isittrue.service.search_web", fail_search),
            patch.object(svc, "_judge", fake_judge),
        ):
            result = asyncio.run(svc.fact_check("某说法"))
        assert result["verdict"] == "unknown"
        assert result["searched"] is True

    def test_plan_failure_falls_back_to_search(self):
        """规划 LLM 失败时兜底为用原文搜索。"""
        svc = IsItTrueService(_config())

        async def fail_llm(*args, **kwargs):
            raise RuntimeError("LLM 挂了")

        with patch(
            "atri_qq_bot.isittrue.service._llm_chat", fail_llm
        ):
            plan = asyncio.run(svc._plan("某说法"))
        assert plan["verdict"] == "needs_search"
        assert plan["search_queries"]


class TestSchemaRegistration:
    def test_schema_present_when_enabled(self):
        from atri_qq_bot.llm_tools.schema import available_tool_schemas

        tools = available_tool_schemas(_config(llm_tools_enabled=True))
        names = [t["function"]["name"] for t in tools]
        assert "fact_check" in names

    def test_schema_absent_when_disabled(self):
        from atri_qq_bot.llm_tools.schema import available_tool_schemas

        tools = available_tool_schemas(
            _config(isittrue_enabled=False, llm_tools_enabled=True)
        )
        names = [t["function"]["name"] for t in tools]
        assert "fact_check" not in names


class TestConfigLoader:
    def test_env_flag_wired(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ISITTRUE_ENABLED", "true")
        from atri_qq_bot.config.loader import load_config

        cfg = load_config(str(tmp_path / ".env"))
        assert cfg.isittrue_enabled is True
        assert cfg.isittrue_timeout_seconds == 120.0
