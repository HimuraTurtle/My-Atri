"""Suwayomi 漫画助手移植的回归测试。

FakeClient 按 MangaService 实际调用的客户端方法提供固定数据，不发真实
网络请求；下载/打包通过 monkeypatch fetch_pages_local 注入本地图片。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from atri_qq_bot.config import BotConfig
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.manga import (
    MangaService,
    get_manga_service,
    reset_manga_service,
)
from atri_qq_bot.manga.models import Chapter, Manga, Source
from atri_qq_bot.manga.service import parse_chapter_number_text, resolve_chapter
from atri_qq_bot.manga.store import MangaStore


def _config(tmp_path: Path, **overrides: Any) -> BotConfig:
    defaults: dict[str, Any] = {
        "bot_qq": 100000001,
        "host": "127.0.0.1",
        "port": 8765,
        "reply_mode": "mention",
        "openai_api_key": "test-key",
        "openai_base_url": "https://example.com/v1",
        "openai_model": "test-model",
        "temperature": 0.8,
        "max_tokens": 350,
        "manga_store_path": tmp_path / "manga" / "store.json",
        "manga_download_format": "zip",
    }
    defaults.update(overrides)
    return BotConfig(**defaults)


def make_chapter(cid: int, num: float, source_order: int | None = None) -> Chapter:
    return Chapter(
        id=cid,
        url=f"https://example.com/ch/{cid}",
        name=f"第{num:g}话",
        chapter_number=num,
        source_order=cid if source_order is None else source_order,
        manga_id=1,
        page_count=2,
    )


class FakeClient:
    """实现 MangaService 依赖的 SuwayomiClient 接口子集。"""

    def __init__(self) -> None:
        self.sources = [
            Source(id="1", name="copy_manga", lang="zh", display_name="拷贝漫画"),
        ]
        self.mangas: dict[int, Manga] = {
            11: Manga(id=11, source_id=1, url="u", title="一拳超人", status="ONGOING"),
            12: Manga(id=12, source_id=1, url="u", title="进击的巨人", status="COMPLETED"),
        }
        self.chapters: dict[int, list[Chapter]] = {
            11: [make_chapter(101, 1.0), make_chapter(102, 2.0), make_chapter(103, 3.0)],
        }
        self.update_library_calls = 0

    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

    async def get_sources(self) -> list[Source]:
        return self.sources

    async def search_manga(self, source_id: str | int, query: str, page: int = 1) -> Any:
        class _Result:
            def __init__(self, mangas: list[Manga]) -> None:
                self.mangas = mangas

        query = query.replace("+", " ").strip()
        matches = [m for m in self.mangas.values() if query and query in m.title]
        return _Result(matches)

    async def get_manga(self, manga_id: int) -> Manga:
        manga = self.mangas.get(int(manga_id))
        if manga is None:
            from atri_qq_bot.manga.client import SuwayomiError

            raise SuwayomiError(f"manga {manga_id} not found")
        return manga

    async def get_chapters(self, manga_id: int) -> list[Chapter]:
        return list(self.chapters.get(int(manga_id), []))

    async def fetch_chapters(self, manga_id: int) -> list[Chapter]:
        return list(self.chapters.get(int(manga_id), []))

    async def update_library(self) -> None:
        self.update_library_calls += 1

    async def search_manga_by_title(self, title: str, limit: int = 10) -> list[Manga]:
        return [m for m in self.mangas.values() if title and title in m.title]


def _service_with_fake_client(config: BotConfig) -> tuple[MangaService, FakeClient]:
    service = MangaService(config)
    fake = FakeClient()
    service.client = fake
    return service, fake


# ── 纯逻辑层 ────────────────────────────────────────────────────────


def test_parse_chapter_number_text_variants() -> None:
    assert parse_chapter_number_text("5") == 5.0
    assert parse_chapter_number_text("第5话") == 5.0
    assert parse_chapter_number_text("第38.5話") == 38.5
    assert parse_chapter_number_text("番外") is None


def test_resolve_chapter_by_number_id_and_ambiguity() -> None:
    chapters = [
        make_chapter(201, 5.0),
        make_chapter(202, 5.0, source_order=202),
        make_chapter(203, 6.0),
    ]
    target, err = resolve_chapter(chapters, "6", "一拳超人", "阅读")
    assert target is not None and target.id == 203
    assert err is None

    target, err = resolve_chapter(chapters, "id:202", "一拳超人", "阅读")
    assert target is not None and target.id == 202

    target, err = resolve_chapter(chapters, "5", "一拳超人", "阅读")
    assert target is None
    assert "ID:201" in err and "ID:202" in err

    target, err = resolve_chapter(chapters, "9", "一拳超人", "阅读")
    assert target is None and "未找到" in err


def test_is_command_detection() -> None:
    assert MangaService.is_command("漫画 搜索 一拳超人")
    assert MangaService.is_command("/漫画 我的订阅")
    assert not MangaService.is_command("漫画")
    assert not MangaService.is_command("漫画跳舞")
    assert not MangaService.is_command("我想看漫画")


def test_store_subscription_roundtrip_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "manga" / "store.json"
    store = MangaStore(path)
    store.subscribe(11, "一拳超人", 1, "group:20001")
    store.subscribe(11, "一拳超人", 1, "group:20001")  # 幂等
    store.subscribe(12, "进击的巨人", 1, "group:20001")

    subs = store.get_subscriptions("group:20001")
    assert {s["manga_id"] for s in subs} == {11, 12}
    assert store.get_subscriptions("group:20002") == []

    store.set_push_default("group:20001", True)
    store.set_auto_push_all("group:20001", True)
    assert store.is_auto_push_enabled(store.get_all_subscriptions(), 11, "group:20001")

    store.update_latest_chapter(11, 102)
    assert path.is_file()

    reloaded = MangaStore(path)
    subs = reloaded.get_subscriptions("group:20001")
    assert subs[0]["latest_chapter_id"] == 102
    assert subs[0]["push_enabled"] is True

    store.unsubscribe(12, "group:20001")
    assert {s["manga_id"] for s in store.get_subscriptions("group:20001")} == {11}
    store.unsubscribe(11, "group:20001")
    assert store.get_subscriptions("group:20001") == []
    assert store.get_all_subscriptions() == {}


# ── 命令层 ──────────────────────────────────────────────────────────


def test_search_then_subscribe_and_list_flow(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"

    result = asyncio.run(service.handle_command("漫画 搜索 一拳超人", conv))
    text = result.messages[0].content
    assert "[1] 一拳超人" in text and "漫画 订阅" in text

    result = asyncio.run(service.handle_command("漫画 订阅 1", conv))
    assert "已订阅「一拳超人」" in result.messages[0].content

    result = asyncio.run(service.handle_command("漫画 我的订阅", conv))
    text = result.messages[0].content
    assert "一拳超人" in text and "ID: 11" in text

    result = asyncio.run(service.handle_command("漫画 订阅 99", conv))
    assert "未找到该编号" in result.messages[0].content

    result = asyncio.run(service.handle_command("漫画 取消订阅 一拳", conv))
    assert "已取消订阅" in result.messages[0].content
    assert service.store.get_subscriptions(conv) == []


def test_push_toggle_command(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"
    service.store.subscribe(11, "一拳超人", 1, conv)

    result = asyncio.run(service.handle_command("漫画 推送 开", conv))
    assert "已开启自动推送" in result.messages[0].content
    assert service.store.is_auto_push_enabled(service.store.get_all_subscriptions(), 11, conv)

    result = asyncio.run(service.handle_command("漫画 推送 关", conv))
    assert "已关闭自动推送" in result.messages[0].content
    assert not service.store.is_auto_push_enabled(service.store.get_all_subscriptions(), 11, conv)


def test_chapters_command_lists_labels(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    result = asyncio.run(service.handle_command("漫画 章节 11", "group:20001"))
    text = "\n".join(m.content for m in result.messages)
    assert "共 3 话" in text
    assert "第1话" in text and "第3话" in text


def test_read_command_delivers_images(tmp_path: Path, monkeypatch) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    images = [tmp_path / f"p{i}.jpg" for i in range(2)]
    for path in images:
        path.write_bytes(b"fake-image")

    async def fake_fetch_pages_local(*args: Any, **kwargs: Any) -> tuple[int, list[str], list[str], Path | None]:
        return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

    monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)

    result = asyncio.run(service.handle_command("漫画 阅读 11 2", "group:20001"))
    kinds = [m.kind for m in result.messages]
    assert kinds[0] == "text" and "一拳超人" in result.messages[0].content
    assert kinds.count("image") == 2
    assert result.cleanup_dirs == [tmp_path]


def test_download_command_packs_zip(tmp_path: Path, monkeypatch) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    images = [tmp_path / f"p{i}.jpg" for i in range(2)]
    for path in images:
        path.write_bytes(b"fake-image")

    async def fake_fetch_pages_local(*args: Any, **kwargs: Any) -> tuple[int, list[str], list[str], Path | None]:
        return len(images), [str(p) for p in images], [str(p) for p in images], tmp_path

    monkeypatch.setattr("atri_qq_bot.manga.fetch_pages_local", fake_fetch_pages_local)

    result = asyncio.run(service.handle_command("漫画 下载 11 1 zip", "group:20001"))
    assert len(result.messages) == 1 and result.messages[0].kind == "file"
    assert result.messages[0].content.endswith(".zip")
    assert Path(result.messages[0].content).is_file()


def test_handle_command_wraps_service_error(tmp_path: Path) -> None:
    service, fake = _service_with_fake_client(_config(tmp_path))

    async def boom(*args: Any, **kwargs: Any) -> list[Chapter]:
        from atri_qq_bot.manga.client import SuwayomiError

        raise SuwayomiError("server down")

    fake.get_chapters = boom  # type: ignore[method-assign]
    fake.fetch_chapters = boom  # type: ignore[method-assign]

    result = asyncio.run(service.handle_command("漫画 章节 11", "group:20001"))
    assert "漫画服务暂时不可用" in result.messages[0].content


# ── LLM 工具层 ──────────────────────────────────────────────────────


def test_tool_schemas_gating() -> None:
    def names(config: BotConfig) -> set[str]:
        return {
            item["function"]["name"]
            for item in available_tool_schemas(config)
        }

    config = _config(Path("C:/nonexistent"))
    manga_names = {
        "suwayomi_search_manga",
        "suwayomi_get_chapters",
        "suwayomi_get_subscriptions",
        "suwayomi_subscribe",
        "suwayomi_unsubscribe",
        "suwayomi_send_chapter",
    }
    assert manga_names <= names(config)

    config = _config(Path("C:/nonexistent"), manga_enabled=False)
    assert not (manga_names & names(config))

    config = _config(Path("C:/nonexistent"), manga_ai_tools_enabled=False)
    assert not (manga_names & names(config))

    config = _config(Path("C:/nonexistent"), llm_tools_enabled=False)
    assert names(config) == set()


def test_tool_search_manga_returns_candidates(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    data = asyncio.run(service.tool_search_manga("一拳"))
    assert data["success"] is True
    assert data["results"][0]["manga_id"] == 11
    assert data["results"][0]["title"] == "一拳超人"

    data = asyncio.run(service.tool_search_manga("不存在的漫画"))
    assert data["success"] is False


def test_tool_get_chapters_and_send_gating(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"

    accepted, note = asyncio.run(service.tool_send_chapter(conv, 11, 102))
    assert accepted is False  # 未先查询章节，防误发闸门拦截

    data = asyncio.run(service.tool_get_chapters(conv, 11, selector="2"))
    assert data["success"] is True
    assert data["selected_chapter"]["chapter_id"] == 102
    assert data["total_chapters"] == 3

    accepted, note = asyncio.run(service.tool_send_chapter(conv, 11, 102))
    assert accepted is True and "第2话" in note

    accepted, note = asyncio.run(service.tool_send_chapter(conv, 11, 999))
    assert accepted is False

    # 其他会话不共享暴露记录
    accepted, _ = asyncio.run(service.tool_send_chapter("group:20002", 11, 102))
    assert accepted is False


def test_tool_get_chapters_auto_picks_story_chapter_on_dup_number(tmp_path: Path) -> None:
    """正篇与番外同号时自动选正篇，避免一轮人肉确认。"""
    service, fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"
    fake.chapters[11] = [
        make_chapter(201, 1.0),
        make_chapter(202, 1.0, source_order=202),
        make_chapter(203, 2.0),
    ]
    fake.chapters[11][1].name = "番外1 一个美丽的错误"
    fake.chapters[11][0].name = "第1话 死神&草莓"

    data = asyncio.run(service.tool_get_chapters(conv, 11, selector="1"))
    assert data["success"] is True
    assert data["selected_chapter"]["chapter_id"] == 201
    assert "正篇" in data.get("note", "")

    accepted, note = asyncio.run(service.tool_send_chapter(conv, 11, 201))
    assert accepted is True


def test_tool_get_chapters_still_asks_when_no_story_match(tmp_path: Path) -> None:
    """全是番外类或多个正篇时仍返回候选，并指导确认后直发。"""
    service, fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"
    fake.chapters[11] = [
        make_chapter(201, 1.0),
        make_chapter(202, 1.0, source_order=202),
    ]
    fake.chapters[11][0].name = "番外A"
    fake.chapters[11][1].name = "番外B"

    data = asyncio.run(service.tool_get_chapters(conv, 11, selector="1"))
    assert data["success"] is False
    assert "suwayomi_send_chapter" in data["error"]
    assert {c["chapter_id"] for c in data["candidates"]} == {201, 202}

    # 多个正篇（无番外标记）时同样回到候选确认
    fake.chapters[11][0].name = "第1话 甲"
    fake.chapters[11][1].name = "第1话 乙"
    data = asyncio.run(service.tool_get_chapters(conv, 11, selector="1"))
    assert data["success"] is False
    assert len(data["candidates"]) == 2


def test_tool_subscription_lifecycle(tmp_path: Path) -> None:
    service, _fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"

    data = asyncio.run(service.tool_subscribe(conv, 11, push_enabled=True))
    assert data["success"] is True
    assert data["manga_id"] == 11

    data = asyncio.run(service.tool_subscribe(conv, 11))
    assert data.get("already_subscribed") is True

    data = asyncio.run(service.tool_get_subscriptions(conv))
    assert data["count"] == 1
    assert data["subscriptions"][0]["push_enabled"] is True

    data = asyncio.run(service.tool_unsubscribe(conv, 11))
    assert data["success"] is True
    data = asyncio.run(service.tool_unsubscribe(conv, 11))
    assert data.get("not_subscribed") is True
    assert service.store.get_subscriptions(conv) == []


# ── 引擎集成层 ──────────────────────────────────────────────────────


def _engine_with_manga(tmp_path: Path, monkeypatch) -> tuple[Any, BotConfig, FakeClient]:
    from atri_qq_bot.persona import AtriReplyEngine

    reset_manga_service()
    config = _config(tmp_path)
    engine = AtriReplyEngine(config)
    service = get_manga_service(config)
    assert service is not None
    fake = FakeClient()
    service.client = fake
    return engine, config, fake


def test_engine_context_tool_dispatches_manga_tools(tmp_path: Path, monkeypatch) -> None:
    engine, config, _fake = _engine_with_manga(tmp_path, monkeypatch)
    conv = "group:20001"

    result = asyncio.run(
        engine._execute_context_tool(conv, "suwayomi_subscribe", {"manga_id": 11}, config)
    )
    assert result is not None
    data = json.loads(result)
    assert data["success"] is True and data["title"] == "一拳超人"

    result = asyncio.run(
        engine._execute_context_tool(conv, "suwayomi_get_subscriptions", {}, config)
    )
    data = json.loads(result)
    assert data["count"] == 1

    result = asyncio.run(
        engine._execute_context_tool(conv, "suwayomi_unknown", {}, config)
    )
    assert result is not None and "未知漫画工具" in result


def test_engine_send_chapter_registers_pending_delivery(tmp_path: Path, monkeypatch) -> None:
    engine, config, _fake = _engine_with_manga(tmp_path, monkeypatch)
    conv = "group:20001"

    result = asyncio.run(
        engine._execute_context_tool(
            conv,
            "suwayomi_get_chapters",
            {"manga_id": 11, "selector": "latest"},
            config,
        )
    )
    assert result is not None and '"success": true' in result

    result = asyncio.run(
        engine._execute_context_tool(
            conv,
            "suwayomi_send_chapter",
            {"manga_id": 11, "chapter_id": 103, "fmt": "pdf"},
            config,
        )
    )
    assert result is not None and "已受理" in result

    delivery = engine.consume_manga_delivery(conv)
    assert delivery is not None
    assert delivery.manga_id == 11 and delivery.chapter_id == 103
    assert delivery.fmt == "pdf"
    assert engine.consume_manga_delivery(conv) is None


def test_engine_send_chapter_blocked_when_disabled(tmp_path: Path, monkeypatch) -> None:
    engine, config, _fake = _engine_with_manga(tmp_path, monkeypatch)
    conv = "group:20001"
    engine.config = _config(tmp_path, manga_allow_ai_send=False)
    engine._manga_disabled_config = engine.config

    service = get_manga_service(config)
    assert service is not None
    service.remember_exposed_chapters(conv, 11, {103})

    result = asyncio.run(
        engine._execute_context_tool(
            conv,
            "suwayomi_send_chapter",
            {"manga_id": 11, "chapter_id": 103},
            engine.config,
        )
    )
    assert result is not None and "不允许主动发送" in result
    assert engine.consume_manga_delivery(conv) is None


def test_update_check_produces_notifications(tmp_path: Path) -> None:
    service, fake = _service_with_fake_client(_config(tmp_path))
    conv = "group:20001"
    service.store.subscribe(11, "一拳超人", 1, conv)
    service.store.update_latest_chapter(11, 101)  # 落后两章

    notifications = asyncio.run(service.check_updates(force=True))
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.conversation_id == conv
    assert notification.items[0]["title"] == "一拳超人"
    assert len(notification.items[0]["chapters"]) == 2  # 102、103 为新章节
    assert fake.update_library_calls == 1

    # 无更新时不产生通知
    notifications = asyncio.run(service.check_updates(force=True))
    assert notifications == []
