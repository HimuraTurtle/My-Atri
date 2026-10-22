"""舞萌 DX 查分器（落雪开发者模式）服务测试。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import atri_qq_bot.maimai
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.maimai import MaimaiService
from atri_qq_bot.maimai import get_maimai_service as _original_get_service
from atri_qq_bot.maimai.service import _to_score_item
from atri_qq_bot.persona.engine_tools import EngineToolsMixin


def _config(**overrides):
    base = {
        "maimai_enabled": True,
        "maimai_lxns_dev_token": "dev-token",
        "maimai_timeout_seconds": 20.0,
        "papers_enabled": False,
        "llm_tools_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _score(rating=530.9, achievements=None, **extra):
    base = {
        "id": 834,
        "song_name": "PANDORA PARADOXXX",
        "level": "15",
        "level_index": 4,
        "achievements": achievements,
        "fc": "app",
        "fs": "fdxp",
        "rate": "sss",
        "dx_score": 1234,
        "dx_star": 5,
        "dx_rating": rating,
        "type": "standard",
    }
    base.update(extra)
    return base


def _player(**extra):
    base = {
        "name": "Ｌｘｎｓ",
        "rating": 11307,
        "friend_code": 123456789000000,
        "course_rank": 21,
        "class_rank": 16,
        "star": 82,
        "trophy": {"name": "真皆传强者"},
        "upload_time": "2026-10-01T00:00:00Z",
    }
    base.update(extra)
    return base


def _bests(standard=None, dx=None):
    return {
        "standard_total": 8000,
        "dx_total": 3307,
        "standard": standard or [],
        "dx": dx or [],
    }


class TestScoreItem:
    def test_approx_achievement_derived_from_rate(self):
        item, approx = _to_score_item(_score())
        assert item["achievement"] == 100.0
        assert approx is True
        assert item["achievement_approx"] is True

    def test_exact_achievement_kept(self):
        item, approx = _to_score_item(_score(achievements=100.4999))
        assert item["achievement"] == 100.4999
        assert approx is False
        assert item["achievement_approx"] is False

    def test_rating_floored(self):
        item, _ = _to_score_item(_score(rating=530.9))
        assert item["rating"] == 530

    def test_difficulty_and_chart_names(self):
        item, _ = _to_score_item(_score(level_index=3, type="dx"))
        assert item["difficulty"] == "MASTER"
        assert item["chart"] == "DX谱"
        item, _ = _to_score_item(_score(level_index=4))
        assert item["difficulty"] == "Re:MASTER"
        assert item["chart"] == "标准谱"

    def test_title_falls_back_to_song_id(self):
        item, _ = _to_score_item(_score(song_name=""))
        assert item["title"] == "曲目#834"

    def test_fc_fs_display_names(self):
        item, _ = _to_score_item(_score(fc="app", fs="fdxp"))
        assert item["fc"] == "AP+"
        assert item["fs"] == "FDX+"


class TestService:
    def test_disabled_returns_none(self):
        assert _original_get_service(_config(maimai_enabled=False)) is None

    def test_empty_token_returns_none(self):
        assert _original_get_service(_config(maimai_lxns_dev_token="  ")) is None

    def test_enabled_returns_singleton(self):
        svc = _original_get_service(_config())
        assert isinstance(svc, MaimaiService)
        assert _original_get_service(_config()) is svc

    def test_invalid_qq_returns_error(self):
        svc = MaimaiService(_config())
        assert asyncio.run(svc.best50_by_qq("not-a-number"))["ok"] is False
        assert asyncio.run(svc.best50_by_qq(999))["ok"] is False
        assert asyncio.run(svc.best50_by_qq(10**12))["ok"] is False

    def test_missing_token_returns_error(self):
        svc = MaimaiService(_config(maimai_lxns_dev_token=""))
        result = asyncio.run(svc.best50_by_qq(123456789))
        assert result["ok"] is False
        assert "开发者 Token" in result["error"]

    def test_success_queries_player_then_bests(self):
        svc = MaimaiService(_config())
        paths = []

        async def fake_fetch(path, headers, timeout):
            paths.append(path)
            assert headers == {"Authorization": "dev-token"}
            if path == "/player/qq/123456789":
                return _player()
            return _bests(standard=[_score()], dx=[_score(rating=200.1, type="dx")])

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(123456789))
        assert paths == [
            "/player/qq/123456789",
            "/player/123456789000000/bests",
        ]
        assert result["ok"] is True
        assert result["mode"] == "b50"
        assert result["player"]["name"] == "Ｌｘｎｓ"
        assert result["player"]["rating"] == 11307
        assert result["player"]["trophy"] == "真皆传强者"
        assert result["standard_total"] == 8000
        assert result["dx_total"] == 3307
        assert result["standard_count"] == 1
        assert result["dx_count"] == 1
        assert result["top_scores"][0]["title"] == "PANDORA PARADOXXX"
        assert result["top_scores"][0]["rating"] == 530
        assert "achievement_approx=true" in result["note"]
        assert "段位" not in result["note"].split("；")[0] or "数字代码" in result["note"]

    def test_ap_mode_hits_ap_endpoint(self):
        svc = MaimaiService(_config())
        paths = []

        async def fake_fetch(path, headers, timeout):
            paths.append(path)
            if path == "/player/qq/12345":
                return _player()
            return _bests()

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(12345, mode="ap50"))
        assert paths[-1] == "/player/123456789000000/bests/ap"
        assert result["mode"] == "ap50"

    def test_missing_friend_code_returns_error(self):
        svc = MaimaiService(_config())

        async def fake_fetch(path, headers, timeout):
            return _player(friend_code=0)

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(123456789))
        assert result["ok"] is False
        assert "好友码" in result["error"]

    def test_top_scores_sorted_and_capped(self):
        svc = MaimaiService(_config())
        standard = [_score(rating=100.0 + i) for i in range(35)]
        dx = [_score(rating=50.0 + i, type="dx") for i in range(15)]

        async def fake_fetch(path, headers, timeout):
            if path == "/player/qq/12345":
                return _player()
            return _bests(standard=standard, dx=dx)

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(12345))
        assert len(result["top_scores"]) == 10
        ratings = [item["rating"] for item in result["top_scores"]]
        assert ratings == sorted(ratings, reverse=True)

    def test_http_404_maps_to_binding_guidance(self):
        from atri_qq_bot.maimai.service import _LxnsApiError

        svc = MaimaiService(_config())

        async def fake_fetch(path, headers, timeout):
            raise _LxnsApiError("该 QQ 号未在落雪查分器绑定；需要玩家先在 maimai.lxns.net 绑定 QQ 号，并在账号设置的隐私设置中允许第三方读取成绩")

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(123456789))
        assert result["ok"] is False
        assert "绑定" in result["error"]

    def test_generic_failure_returns_error_without_raising(self):
        svc = MaimaiService(_config())

        async def fake_fetch(path, headers, timeout):
            raise RuntimeError("connection refused")

        svc._fetch = fake_fetch
        result = asyncio.run(svc.best50_by_qq(123456789))
        assert result["ok"] is False
        assert "落雪接口调用失败" in result["error"]


class TestSchema:
    def test_schema_registered_with_token(self):
        names = [t["function"]["name"] for t in available_tool_schemas(_config())]
        assert "search_maimai_best50" in names

    def test_schema_absent_without_token(self):
        names = [
            t["function"]["name"]
            for t in available_tool_schemas(_config(maimai_lxns_dev_token=""))
        ]
        assert "search_maimai_best50" not in names

    def test_schema_absent_when_disabled(self):
        names = [
            t["function"]["name"]
            for t in available_tool_schemas(_config(maimai_enabled=False))
        ]
        assert "search_maimai_best50" not in names

    def test_schema_contract(self):
        tool = next(
            t
            for t in available_tool_schemas(_config())
            if t["function"]["name"] == "search_maimai_best50"
        )
        params = tool["function"]["parameters"]
        assert params.get("required") is None
        assert params["properties"]["mode"]["enum"] == ["b50", "ap50"]
        assert params["additionalProperties"] is False


class TestExecutor:
    def _engine(self):
        class Holder(EngineToolsMixin):
            pass

        return Holder()

    def test_disabled_returns_notice(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_maimai_best50_tool(
                "private:123456789", {}, _config(maimai_enabled=False)
            )
        )
        assert "未启用" in result

    def test_group_chat_without_qq_asks_for_it(self):
        engine = self._engine()
        result = asyncio.run(
            engine._execute_maimai_best50_tool("group:999", {}, _config())
        )
        assert "询问 QQ 号" in result

    def test_private_chat_defaults_to_user_qq(self, monkeypatch):
        engine = self._engine()
        captured = {}

        class StubService:
            async def best50_by_qq(self, qq, mode="b50"):
                captured["qq"] = qq
                captured["mode"] = mode
                return {"ok": True, "player": {"name": "测试"}}

        monkeypatch.setattr(
            atri_qq_bot.maimai, "get_maimai_service", lambda config: StubService()
        )
        result = asyncio.run(
            engine._execute_maimai_best50_tool("private:123456789", {}, _config())
        )
        payload = json.loads(result)
        assert payload["ok"] is True
        assert captured["qq"] == 123456789

    def test_explicit_qq_and_mode_normalized(self, monkeypatch):
        engine = self._engine()
        captured = {}

        class StubService:
            async def best50_by_qq(self, qq, mode="b50"):
                captured["qq"] = qq
                captured["mode"] = mode
                return {"ok": True}

        monkeypatch.setattr(
            atri_qq_bot.maimai, "get_maimai_service", lambda config: StubService()
        )
        asyncio.run(
            engine._execute_maimai_best50_tool(
                "group:999", {"qq": 987654321, "mode": "AP50"}, _config()
            )
        )
        assert captured == {"qq": 987654321, "mode": "ap50"}

        asyncio.run(
            engine._execute_maimai_best50_tool(
                "group:999", {"qq": 987654321, "mode": "bogus"}, _config()
            )
        )
        assert captured["mode"] == "b50"

    def test_service_error_bubbles_as_notice(self, monkeypatch):
        engine = self._engine()

        class BrokenService:
            async def best50_by_qq(self, qq, mode="b50"):
                raise RuntimeError("boom")

        monkeypatch.setattr(
            atri_qq_bot.maimai, "get_maimai_service", lambda config: BrokenService()
        )
        result = asyncio.run(
            engine._execute_maimai_best50_tool(
                "private:123456789", {}, _config()
            )
        )
        assert "舞萌查分执行失败" in result
        assert "不要编造" in result
