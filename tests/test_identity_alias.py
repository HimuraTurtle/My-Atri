"""人级别身份别名与说话人锚点测试。

背景（2026-11 移植评估）：记忆条目归属只存瞬时昵称文本，群友改名后
同一个人裂成两个「人」；检索注入也不绑定当前说话人，模型容易把
检索命中的他人事实安到当前说话人头上。本组测试覆盖：
- AliasRegistry：昵称登记/改名/亲口教的称呼/曾用名
- recall_context：群主会话的「当前发言人」锚点（含曾用名对照）
- L1 条目落盘 actor_id，渲染端按注册表解析回当前称呼
- living_memory：总结带参与者标注，注入渲染「与XX有关」
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.memory.alias_registry import AliasRegistry
from atri_qq_bot.retrieval.planner import ContextRetrievalPlanner, _memory_line
from atri_qq_bot.retrieval.repository import RetrievedEntry
from atri_qq_bot.living_memory.summarizer import _collect_participants


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        memory_extraction_enabled=True,
        memory_extraction_llm_enabled=False,
    )


def _wait_for(path, predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if predicate(data):
                return data
        time.sleep(0.02)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert predicate(data), data
    return data


# ------------------------------------------------------------ AliasRegistry


def test_alias_registry_records_renames(tmp_path) -> None:
    registry = AliasRegistry(tmp_path / "aliases.json")
    registry.record(12345, "阿明")
    assert registry.display(12345) == "阿明"
    registry.record(12345, "明哥")
    assert registry.display(12345) == "明哥"
    assert registry.former_names(12345) == ["阿明"]
    # 持久化后重开仍在
    registry2 = AliasRegistry(tmp_path / "aliases.json")
    assert registry2.display(12345) == "明哥"
    assert registry2.former_names(12345) == ["阿明"]


def test_alias_registry_preferred_beats_nickname(tmp_path) -> None:
    registry = AliasRegistry(tmp_path / "aliases.json")
    registry.record(999, "群名片名")
    registry.set_preferred(999, "小明")
    assert registry.display(999) == "小明"
    # 后续观察到的昵称不覆盖亲口教的称呼
    registry.record(999, "又改名了")
    assert registry.display(999) == "小明"
    assert "又改名了" in registry.former_names(999, limit=5)


def test_alias_registry_rejects_garbage(tmp_path) -> None:
    registry = AliasRegistry(tmp_path / "aliases.json")
    assert registry.record(None, "阿明") is False
    assert registry.record(1, "") is False
    # 纯数字长串多半是 QQ 号回显，不当称呼
    assert registry.record(2, "12345678") is False
    assert registry.set_preferred(3, "123456") is False


# ------------------------------------------------------------- 说话人锚点


def test_group_main_recall_has_speaker_anchor(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json", _config())
    store.observe_user(
        "group:7", "我生日是3月5日", now=1000, actor_id=111,
        nickname="群友甲", visibility="group:7", source_context="group:7",
    )
    context = store.recall_context(
        "group:7", "最近怎么样", speaker_name="群友乙", speaker_id=222
    )
    assert "当前正在和你说话的群友是“群友乙”" in context
    # 甲的记忆以归属标注出现，锚点提醒不要安到乙头上
    assert "不要安到 TA 头上" in context


def test_speaker_anchor_shows_former_names(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json", _config())
    store.aliases.record(333, "阿明")
    store.aliases.record(333, "明哥")
    context = store.recall_context(
        "group:7", "在吗", speaker_name="明哥", speaker_id=333
    )
    assert "曾用名/别称：阿明" in context
    assert "是同一个人" in context


def test_member_scope_and_private_have_no_anchor(tmp_path) -> None:
    store = UserMemoryStore(tmp_path / "users.json", _config())
    member = store.recall_context(
        "group:7:user:111", "在吗", speaker_name="群友甲", speaker_id=111
    )
    assert "当前正在和你说话的群友" not in (member or "")
    private = store.recall_context(
        "private:111", "在吗", speaker_name="群友甲", speaker_id=111
    )
    assert "当前正在和你说话的群友" not in (private or "")


# --------------------------------------------------- actor_id 落盘与渲染解析


def test_group_l1_entries_carry_actor_id(tmp_path) -> None:
    path = tmp_path / "users.json"
    store = UserMemoryStore(path, _config())
    for i in range(2):
        store.observe_user(
            "group:7", "以后叫我老明", now=1000 + i, actor_id=555,
            nickname="阿明", visibility="group:7", source_context="group:7",
        )
    data = _wait_for(
        path,
        lambda d: bool(
            (d.get("conversations", {}).get("group:7", {})
             .get("structured_memory", {}).get("l1"))
        ),
    )
    l1 = data["conversations"]["group:7"]["structured_memory"]["l1"]
    assert all(entry.get("actor_id") == "555" for entry in l1)
    # 「叫我 XX」被识别为称呼并写入人级别注册表
    assert store.aliases.display(555) == "老明"


def _entry(metadata: dict) -> RetrievedEntry:
    return RetrievedEntry(
        entry_id="l1:test",
        scope="group:7",
        source="user",
        layer="L1",
        title="称呼",
        content="自称阿明",
        importance=0.7,
        updated_at=1000.0,
        score=1.0,
        metadata=metadata,
    )


def test_memory_line_resolves_renamed_actor(tmp_path) -> None:
    registry = AliasRegistry(tmp_path / "aliases.json")
    registry.record(666, "阿明")
    registry.record(666, "明哥")
    entry = _entry({"actor_name": "阿明", "actor_id": "666"})
    line = _memory_line(
        entry, group_main=True,
        alias_resolver=lambda actor_id, remembered: registry.resolve_display_name(
            actor_id, remembered
        ),
    )
    # 记忆里存的是旧昵称，渲染解析回当前称呼（主语标注用现名）
    assert line.startswith("群友“明哥”")
    assert "群友“阿明”" not in line
    # 无解析器时保持旧昵称原样（向后兼容）
    fallback = _memory_line(entry, group_main=True)
    assert "阿明" in fallback


def test_planner_accepts_optional_alias_resolver(tmp_path) -> None:
    from atri_qq_bot.retrieval.planner import RetrievalSettings

    planner = ContextRetrievalPlanner(
        RetrievalSettings(enabled=False, database_path=tmp_path / "sem.sqlite3"),
        alias_resolver=lambda actor_id, name: "解析名",
    )
    assert planner._alias_resolver is not None


# ------------------------------------------------------- living_memory 参与者


def test_collect_participants_dedupes_and_skips_bot() -> None:
    messages = [
        {"role": "user", "sender_name": "阿明"},
        {"role": "user", "sender_name": "阿明"},
        {"role": "assistant", "sender_name": "亚托莉"},
        {"role": "user", "sender_name": "小红"},
        {"role": "user", "sender_name": ""},  # 无名 → 跳过
        {"role": "user", "sender_name": "123456"},  # QQ 号回显 → 跳过
    ]
    assert _collect_participants(messages) == ["阿明", "小红"]


def test_lm_source_label_includes_participants() -> None:
    from atri_qq_bot.living_memory import _source_label

    row = {
        "source_label": "05月12日",
        "participants": ["阿明", "小红"],
    }
    label = _source_label(row)
    assert "与阿明、小红有关" in label
    assert "05月12日" in label
    # 无参与者的旧格式保持不变
    legacy = _source_label({"source_label": "05月12日"})
    assert legacy == "（05月12日）"


def test_lm_store_roundtrips_participants(tmp_path) -> None:
    from atri_qq_bot.living_memory.atoms import MemoryAtom
    from atri_qq_bot.living_memory.store import LivingMemoryStore

    store = LivingMemoryStore(tmp_path / "lm.sqlite3")
    atom = MemoryAtom(
        session_id="group:7",
        content="阿明和小红聊了螃蟹",
        participants=["阿明", "小红"],
        source_label="05月12日",
    )
    memory_id = store.add_memory(atom, None)
    rows = store.search_fts(["螃蟹"], "group:7", limit=3)
    assert rows, "FTS 应能命中刚写入的记忆"
    assert rows[0].get("participants") == ["阿明", "小红"]
    assert memory_id > 0
