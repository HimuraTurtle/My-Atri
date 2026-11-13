"""自然语言看漫画请求拦截器（nl_read + OneBot 接线）的回归测试。

实测（2026-11-12 reply-events）聊天模型对「atri我想看死神BLEACH的
第233话」只口头承诺不调工具，漫画永远不送达；这里锁定确定性拦截
路径的解析边界与端到端投递。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atri_qq_bot.config import BotConfig
from atri_qq_bot.manga import MangaService, reset_manga_service
from atri_qq_bot.manga.alias import expand_title_aliases_sync_parse
from atri_qq_bot.manga.nl_read import parse_read_request
from atri_qq_bot.manga.service import title_matches_query
from atri_qq_bot.onebot.server_parts.server_manga import OneBotMangaMixin

from test_manga import FakeClient, _config, make_chapter


@pytest.fixture(autouse=True)
def _no_llm_sidecars(monkeypatch):
    """默认切断译名扩展与反馈润色的真实 LLM 调用（需要的用例自行覆盖）。"""

    async def fake_expand(config, title):
        return []

    async def fake_style(config, text):
        return text

    monkeypatch.setattr("atri_qq_bot.manga.expand_title_aliases", fake_expand)
    monkeypatch.setattr("atri_qq_bot.manga.style_feedback", fake_style)


# ── 解析层 ──────────────────────────────────────────────────────────


class TestParseReadRequest:
    def test_real_utterances_from_reply_events(self) -> None:
        # 2026-11-12 实测原话：当时模型只回「我去找找」却零工具调用。
        assert parse_read_request("atri我想看死神BLEACH的第233话") == ("死神BLEACH", 233)
        assert parse_read_request("atri我想看学生会也有洞的第一话") == ("学生会也有洞", 1)

    def test_variants_of_intent_and_number(self) -> None:
        assert parse_read_request("我想看《海贼王》第1000话") == ("海贼王", 1000)
        assert parse_read_request("想看海贼王的第一百二十三话") == ("海贼王", 123)
        assert parse_read_request("给我发咒术回战第2话") == ("咒术回战", 2)
        assert parse_read_request("我看下一拳超人的第2话") == ("一拳超人", 2)
        assert parse_read_request("来点间谍过家家的第两百零一集") == ("间谍过家家", 201)
        # 标题带空白要压缩成单 token（合成命令格式约束）。
        assert parse_read_request("想看 进击的巨人 第5话") == ("进击的巨人", 5)
        # 标题内部含「的」不误切。
        assert parse_read_request("想看我的英雄学院第3话") == ("我的英雄学院", 3)
        # 多句消息中后置的请求仍能命中。
        assert parse_read_request("今天好累。我想看一拳超人的第2话") == ("一拳超人", 2)
        # 方括号包裹的标题两侧都要剥干净。
        assert parse_read_request("想看【一拳超人】第5话") == ("一拳超人", 5)

    def test_negation_not_intercepted(self) -> None:
        assert parse_read_request("不想看死神第3话") is None
        assert parse_read_request("别想看一拳超人第3话") is None
        assert parse_read_request("没想看死神第3话") is None
        # 否定后另起一个真实请求仍应命中第二个。
        assert parse_read_request("不想看死神第3话，还是给我看一拳超人第5话吧") == (
            "一拳超人",
            5,
        )

    def test_generic_title_and_chitchat_not_intercepted(self) -> None:
        assert parse_read_request("我想看漫画第3话") is None
        assert parse_read_request("我想看动漫第3话") is None
        assert parse_read_request("看两小时漫画") is None
        assert parse_read_request("这漫画有女主角吗") is None
        assert parse_read_request("漫画好看吗") is None
        # 单字符标题无法定位作品，放行给模型。
        assert parse_read_request("想看A的第3话") is None
        # 话数为零/非法不触发。
        assert parse_read_request("想看海贼王第零话") is None

    def test_empty_and_non_string_input(self) -> None:
        assert parse_read_request("") is None
        assert parse_read_request(None) is None  # type: ignore[arg-type]


# ── OneBot 接线层 ───────────────────────────────────────────────────


class _Holder(OneBotMangaMixin):
    """只依赖漫画 mixin 的最小服务端替身。"""

    def __init__(self, config: BotConfig, service: MangaService) -> None:
        self.config = config
        self._manga_service = service
        self.sent: list[tuple[str, str]] = []
        self.ledger: list[str] = []
        self.reply_engine = SimpleNamespace(
            record_bot_reply=lambda *args, **kwargs: None,
            record_completed_action=lambda conv, summary, action=None: self.ledger.append(
                summary
            ),
        )

    def _delivery_model_name(self, model_type: str) -> str:
        return "test-model"

    def _mark_smart_group_reply(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def send_reply(
        self, websocket: Any, event: dict[str, Any], messages: list[Any],
        model_type: str | None = None, model_name: str | None = None,
    ) -> str:
        for message in messages:
            self.sent.append((message.kind, message.content))
        return "ok"


def _holder_with_service(tmp_path: Path) -> tuple[_Holder, FakeClient]:
    reset_manga_service()
    config = _config(tmp_path)
    service = MangaService(config)
    fake = FakeClient()
    service.client = fake
    return _Holder(config, service), fake


class TestMaybeHandleMangaReadRequest:
    def test_nl_request_synthesizes_read_command_and_delivers(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        holder, _fake = _holder_with_service(tmp_path)
        images = [tmp_path / f"p{i}.jpg" for i in range(2)]
        for path in images:
            path.write_bytes(b"fake-image")

        async def fake_fetch_pages_local(*args: Any, **kwargs: Any):
            return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

        monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "atri我想看一拳超人的第2话", "group:20001"
            )
        )

        assert handled is True
        kinds = [kind for kind, _ in holder.sent]
        assert kinds[0] == "text" and "一拳超人" in holder.sent[0][1]
        assert kinds.count("image") == 2
        # 合成命令同样记入任务账本，供后续聊天引用。
        assert holder.ledger == ["做了漫画「一拳超人 2」的阅读"]

    def test_unknown_title_returns_honest_notice(self, tmp_path: Path) -> None:
        holder, _fake = _holder_with_service(tmp_path)
        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "我想看不存在的作品名的第1话", "group:20001"
            )
        )
        assert handled is True
        assert len(holder.sent) == 1 and holder.sent[0][0] == "text"
        # 反馈按 ATRI 说话方式：口语短句 + 给台阶，不是客服腔
        assert "没搜到" in holder.sent[0][1]
        assert "漫画 搜索" in holder.sent[0][1]
        assert "未找到该漫画" not in holder.sent[0][1]

    def test_unsubscribed_title_falls_back_to_online_source(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """未订阅作品本地库搜不到，回退在线源搜索后照常投递。

        实测（2026-11-12 21:29）「死神bleach」「学生会也有洞」均未订阅，
        resolve_manga 仅查本地库永远「未找到」——在线源回退根治。
        """
        holder, fake = _holder_with_service(tmp_path)
        from atri_qq_bot.manga.models import Manga

        fake.online_mangas[31] = Manga(
            id=31, source_id=1, url="u", title="死神BLEACH", status="ONGOING"
        )
        fake.chapters[31] = [make_chapter(301, 233.0)]
        images = [tmp_path / f"p{i}.jpg" for i in range(2)]
        for path in images:
            path.write_bytes(b"fake-image")

        async def fake_fetch_pages_local(*args: Any, **kwargs: Any):
            return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

        monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "atri我想看死神bleach的第233话", "group:20001"
            )
        )

        assert handled is True
        kinds = [kind for kind, _ in holder.sent]
        assert kinds[0] == "text" and "死神BLEACH" in holder.sent[0][1]
        assert kinds.count("image") == 2

    def test_online_first_result_mismatch_rejected(self, tmp_path: Path) -> None:
        """源端模糊搜索返回不相关首结果（实测 GoDa 搜「学生会也有洞」
        返回「脑洞学生会」）时拒绝投递，宁可不发也不发错的。"""
        holder, fake = _holder_with_service(tmp_path)
        from atri_qq_bot.manga.models import Manga

        junk = Manga(id=99, source_id=1, url="u", title="脑洞学生会", status="ONGOING")

        async def fake_best_match(name, source_filter=None):
            return junk, None

        holder._manga_service.search_best_match = fake_best_match  # type: ignore[method-assign]

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "atri我想看学生会也有洞的第二话", "group:20001"
            )
        )
        assert handled is True
        assert len(holder.sent) == 1 and holder.sent[0][0] == "text"
        assert "没搜到" in holder.sent[0][1]
        assert holder.sent[0][1].count("image") == 0


# ── 译名扩展 ────────────────────────────────────────────────────────


class TestAliasExpansion:
    def test_parse_variants(self) -> None:
        assert expand_title_aliases_sync_parse(
            '["脑洞学生会", "生徒会にも穴はある"]', "学生会也有洞"
        ) == ["脑洞学生会", "生徒会にも穴はある"]
        # markdown 围栏与前后说明文字都能剥掉
        assert expand_title_aliases_sync_parse(
            '```json\n["脑洞学生会"]\n```', "学生会也有洞"
        ) == ["脑洞学生会"]
        assert expand_title_aliases_sync_parse(
            '结果：["脑洞学生会"] 以上', "学生会也有洞"
        ) == ["脑洞学生会"]

    def test_parse_rejects_junk(self) -> None:
        assert expand_title_aliases_sync_parse("[]", "学生会也有洞") == []
        assert expand_title_aliases_sync_parse("不是JSON", "学生会也有洞") == []
        # 过短（<2）与过长（>40）都不要
        assert (
            expand_title_aliases_sync_parse(
                '["会", "' + "长" * 41 + '"]', "学生会也有洞"
            )
            == []
        )
        # 与原标题归一化后相同的别名排除
        assert expand_title_aliases_sync_parse(
            '["学生会也有洞！", "脑洞学生会"]', "学生会也有洞"
        ) == ["脑洞学生会"]
        # 非字符串项跳过
        assert expand_title_aliases_sync_parse(
            '[1, null, "脑洞学生会"]', "学生会也有洞"
        ) == ["脑洞学生会"]

    def test_parse_caps_at_four(self) -> None:
        assert expand_title_aliases_sync_parse(
            '["a1", "b2", "c3", "d4", "e5"]', "原名"
        ) == ["a1", "b2", "c3", "d4"]


# ── 人设润色 ────────────────────────────────────────────────────────


class TestStyleFeedback:
    def _patch_model(self, monkeypatch, value_or_exc):
        async def fake_call(config, system, user, temperature=0.2):
            if isinstance(value_or_exc, Exception):
                raise value_or_exc
            return value_or_exc

        monkeypatch.setattr("atri_qq_bot.manga.style.call_chat_model", fake_call)

    def test_adopt_good_rewrite(self, monkeypatch) -> None:
        from atri_qq_bot.manga.style import style_feedback

        self._patch_model(monkeypatch, "  「没找到这本书啦，换个名字喊我？」  ")
        styled = asyncio.run(style_feedback(None, "诶？这本没搜到呀"))
        assert styled == "没找到这本书啦，换个名字喊我？"

    def test_fallback_on_failure_or_bad_output(self, monkeypatch) -> None:
        from atri_qq_bot.manga.style import style_feedback

        original = "唔…漫画服务好像连不上"
        # 模型抛异常 / 返回空 / 超长跑题，全部回退原文
        for bad in (RuntimeError("boom"), "", "跑" * 201):
            self._patch_model(monkeypatch, bad)
            assert asyncio.run(style_feedback(None, original)) == original

    def test_empty_input_short_circuits(self, monkeypatch) -> None:
        from atri_qq_bot.manga.style import style_feedback

        called = []

        async def fake_call(*args, **kwargs):
            called.append(1)
            return "x"

        monkeypatch.setattr("atri_qq_bot.manga.style.call_chat_model", fake_call)
        assert asyncio.run(style_feedback(None, "  ")) == ""
        assert called == []


# ── 标题归一化互验 ──────────────────────────────────────────────────


class TestTitleMatchesQuery:
    def test_real_probe_pairs(self) -> None:
        # 2026-11-12 真实源探测结果：大小写、斜杠、全半角标点不挡路。
        assert title_matches_query("死神bleach", "死神/BLEACH（全彩版）") is True
        assert title_matches_query("死神bleach", "死神") is True  # 标题是查询子串
        assert title_matches_query("学生会也有洞", "脑洞学生会") is False
        assert title_matches_query("学生会也有洞", "山田君与7位魔女") is False

    def test_punctuation_and_case_insensitive(self) -> None:
        assert title_matches_query("一拳超人", "一拳超人（番外篇）") is True
        assert title_matches_query("BLEACH", "bleach") is True
        assert title_matches_query("间谍过家家", "SPY×FAMILY 间谍过家家") is True

    def test_too_short_or_empty_never_matches(self) -> None:
        assert title_matches_query("死", "死神") is False
        assert title_matches_query("", "死神") is False
        assert title_matches_query("死神", "") is False
        assert title_matches_query("死神", "///") is False


    def test_alternate_translation_accepted_via_alias_review(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """首查结果是同一作品的另一译名（「学生会也有洞」源上叫
        「脑洞学生会」）时，模型译名扩展后复验命中，正常投递。"""
        holder, fake = _holder_with_service(tmp_path)
        from atri_qq_bot.manga.models import Manga

        target = Manga(id=41, source_id=1, url="u", title="脑洞学生会", status="ONGOING")
        fake.chapters[41] = [make_chapter(401, 2.0)]
        images = [tmp_path / f"p{i}.jpg" for i in range(2)]
        for path in images:
            path.write_bytes(b"fake-image")

        async def fake_fetch_pages_local(*args: Any, **kwargs: Any):
            return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

        async def fake_best_match(name, source_filter=None):
            return target, None

        async def fake_expand(config, title):
            return ["脑洞学生会"]

        monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)
        monkeypatch.setattr(
            "atri_qq_bot.manga.expand_title_aliases", fake_expand
        )
        holder._manga_service.search_best_match = fake_best_match  # type: ignore[method-assign]

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "atri我想看学生会也有洞的第二话", "group:20001"
            )
        )

        assert handled is True
        kinds = [kind for kind, _ in holder.sent]
        assert kinds[0] == "text" and "脑洞学生会" in holder.sent[0][1]
        assert kinds.count("image") == 2
        assert "没搜到" not in holder.sent[0][1]

    def test_alias_search_hits_when_first_query_misses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """首查无结果时用别名重新查询命中（标题在源上只用译名存在）。"""
        holder, fake = _holder_with_service(tmp_path)
        from atri_qq_bot.manga.models import Manga

        target = Manga(id=41, source_id=1, url="u", title="脑洞学生会", status="ONGOING")
        fake.chapters[41] = [make_chapter(401, 2.0)]
        images = [tmp_path / f"p{i}.jpg" for i in range(2)]
        for path in images:
            path.write_bytes(b"fake-image")

        async def fake_fetch_pages_local(*args: Any, **kwargs: Any):
            return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

        async def fake_best_match(name, source_filter=None):
            if name == "脑洞学生会":
                return target, None
            return None, "未找到匹配结果"

        async def fake_expand(config, title):
            return ["脑洞学生会"]

        monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)
        monkeypatch.setattr(
            "atri_qq_bot.manga.expand_title_aliases", fake_expand
        )
        holder._manga_service.search_best_match = fake_best_match  # type: ignore[method-assign]

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "我想看学生会也有洞的第2话", "group:20001"
            )
        )

        assert handled is True
        kinds = [kind for kind, _ in holder.sent]
        assert kinds[0] == "text" and "脑洞学生会" in holder.sent[0][1]
        assert kinds.count("image") == 2


    def test_feedback_styled_by_persona_model(self, tmp_path: Path, monkeypatch) -> None:
        """反馈发出前必须经过角色卡润色（模型改写的结果优先采用）。"""
        holder, _fake = _holder_with_service(tmp_path)
        calls: list[str] = []

        async def fake_style(config, text):
            calls.append(text)
            return "呜哇…翻遍书架也没找到这本呀！要不要试试「漫画 搜索」自己挑一挑"

        monkeypatch.setattr("atri_qq_bot.manga.style_feedback", fake_style)

        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "我想看不存在的作品名的第1话", "group:20001"
            )
        )

        assert handled is True
        assert calls and "没搜到" in calls[0]  # 原始反馈进了润色器
        assert holder.sent[0][1].startswith("呜哇")  # 用户收到的是改写结果


# ── 命令与闲聊边界 ──────────────────────────────────────────────────


class TestCommandBoundaries:
    def test_formal_command_not_intercepted(self, tmp_path: Path) -> None:
        holder, _fake = _holder_with_service(tmp_path)
        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "漫画 搜索 一拳超人", "group:20001"
            )
        )
        assert handled is False
        assert holder.sent == []

    def test_non_read_chitchat_passes_through(self, tmp_path: Path) -> None:
        holder, _fake = _holder_with_service(tmp_path)
        event = {"message_type": "group", "group_id": 20001}
        handled = asyncio.run(
            holder._maybe_handle_manga_read_request(
                object(), event, "亚托莉，这漫画有女主角吗", "group:20001"
            )
        )
        assert handled is False
        assert holder.sent == []
