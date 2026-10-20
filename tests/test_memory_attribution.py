"""群记忆归属修复单测：群主会话的记忆条目必须记录发言者，
渲染时按归属标注主语，杜绝把群友A的事安到群友B头上。"""
from __future__ import annotations

import time
from types import SimpleNamespace

from atri_qq_bot.memory import UserMemoryStore
from atri_qq_bot.memory.rules import _format_recall_context
from atri_qq_bot.retrieval.planner import _memory_line
from atri_qq_bot.retrieval.repository import RetrievedEntry


def _memory_config() -> SimpleNamespace:
    return SimpleNamespace(
        memory_extraction_enabled=True,
        memory_extraction_llm_enabled=False,
    )


def _wait_for(path, predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                import json

                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if predicate(data):
                return data
        time.sleep(0.02)
    import json

    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    assert predicate(data), data
    return data


def _group_profile(tmp_path, observe: bool = True):
    path = tmp_path / "users.json"
    store = UserMemoryStore(path, _memory_config())
    if observe:
        store.observe_user(
            "group:777",
            "我生日是3月5日",
            now=1000,
            actor_id=111,
            nickname="群友甲",
            visibility="group:777",
            source_context="group:777",
        )
        saved = _wait_for(
            path,
            lambda data: bool(
                data.get("conversations", {})
                .get("group:777", {})
                .get("structured_memory", {})
                .get("l3")
            ),
        )
        assert saved is not None
    return store


def test_group_main_memory_entries_carry_actor_name(tmp_path) -> None:
    store = _group_profile(tmp_path)
    profile = store.profile("group:777")
    memory = profile["structured_memory"]

    l3 = memory.get("l3") or []
    assert l3, "群主会话 L3 应记录该消息"
    assert all(entry.get("actor_name") == "群友甲" for entry in l3)

    for bucket in ("l1", "candidates"):
        for entry in memory.get(bucket) or []:
            if "3月5日" in str(entry.get("value") or ""):
                assert entry.get("actor_name") == "群友甲", (
                    f"{bucket} 条目缺归属：{entry}"
                )


def test_member_session_entries_do_not_need_actor(tmp_path) -> None:
    path = tmp_path / "users.json"
    store = UserMemoryStore(path, _memory_config())
    store.observe_user(
        "group:777:user:111",
        "我生日是3月5日",
        now=1000,
        actor_id=111,
        nickname="群友甲",
    )
    saved = _wait_for(
        path,
        lambda data: bool(
            data.get("conversations", {})
            .get("group:777:user:111", {})
            .get("structured_memory", {})
            .get("l3")
        ),
    )
    memory = saved["conversations"]["group:777:user:111"]["structured_memory"]
    assert memory["l3"], "成员子会话 L3 应记录该消息"
    # 成员子会话条目属于本人，无需 actor_name；有也不影响渲染。


def test_group_main_recall_renders_actor_subject() -> None:
    profile = {
        "conversation_id": "group:777",
        "structured_memory": {
            "l1": [{"key": "生日", "value": "3月5日", "actor_name": "群友甲"}],
            "l2": [],
            "candidates": [],
            "l3": [{"text": "我生日是3月5日", "actor_name": "群友甲"}],
        },
        "group_activity_state": "活跃",
    }
    text = _format_recall_context(profile, "生日")

    assert "群友“群友甲”" in text, text
    assert "群友“群友甲”说过：我生日是3月5日" in text, text
    assert "你的细碎日常" not in text, "群主会话不得使用指向当前说话者的私聊措辞"
    assert "不能把某位群友说过、做过的事安到当前正在说话的群友头上" in text, text


def test_group_main_legacy_entries_render_as_someone_else() -> None:
    """无归属的历史旧条目也绝不能渲染成“用户/你”。"""
    profile = {
        "conversation_id": "group:777",
        "structured_memory": {
            "l1": [{"key": "生日", "value": "3月5日"}],
            "l2": [],
            "candidates": [],
            "l3": [{"text": "我生日是3月5日"}],
        },
    }
    text = _format_recall_context(profile, "生日")

    assert "有位群友" in text, text
    assert "群友“”" not in text, text
    assert "你的细碎日常" not in text
    assert "你知道的用户信息" not in text, "旧措辞不得再出现在群主会话渲染"


def test_private_recall_keeps_user_wording() -> None:
    profile = {
        "conversation_id": "private:10001",
        "structured_memory": {
            "l1": [{"key": "生日", "value": "3月5日"}],
            "l2": [],
            "candidates": [],
            "l3": [{"text": "我生日是3月5日"}],
        },
        "affection_state": "普通",
    }
    text = _format_recall_context(profile, "生日")

    assert "你的细碎日常" in text, "私聊渲染保持原措辞"
    assert "用户" in text
    assert "有位群友" not in text


def test_planner_memory_line_group_subject() -> None:
    entry = RetrievedEntry(
        entry_id="e1",
        scope="group:777",
        source="user_profile",
        layer="l1",
        title="生日",
        content="3月5日",
        importance=0.9,
        updated_at=0,
        score=1.0,
        metadata={"actor_name": "群友甲"},
    )
    assert _memory_line(entry, group_main=True) == "群友“群友甲”：生日—3月5日"

    legacy = RetrievedEntry(
        entry_id="e2",
        scope="group:777",
        source="user_profile",
        layer="l1",
        title="生日",
        content="3月5日",
        importance=0.9,
        updated_at=0,
        score=1.0,
        metadata={},
    )
    assert _memory_line(legacy, group_main=True) == "某位群友：生日—3月5日"

    private = RetrievedEntry(
        entry_id="e3",
        scope="private:10001",
        source="user_profile",
        layer="l1",
        title="生日",
        content="3月5日",
        importance=0.9,
        updated_at=0,
        score=1.0,
        metadata={},
    )
    assert _memory_line(private, group_main=False) == "生日—3月5日"
