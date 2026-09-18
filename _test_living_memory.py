# -*- coding: utf-8 -*-
"""LivingMemory 端到端接线验证。

覆盖：消息喂入（含成员子会话去重）-> 后台线程攒批总结（假 LLM）->
recall_context 注入 -> recall_living_memory 工具召回 -> TTL 生命周期维护 ->
config=None 不拉起后台线程。只使用临时目录与本地 SQLite，不发真实网络请求。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from atri_qq_bot.config.schema import BotConfig
from atri_qq_bot.llm_tools.schema import available_tool_schemas
from atri_qq_bot.living_memory.store import LivingMemoryStore
from atri_qq_bot.living_memory.summarizer import parse_summary, try_fix_json
from atri_qq_bot.memory import UserMemoryStore

FAKE_SUMMARY = json.dumps(
    {
        "summary": "大家聊了周末去漫展的计划，主人说要cos蓝色的角色，还约好提前一起买票。",
        "topics": ["漫展", "cos", "买票"],
        "key_facts": ["主人周末去漫展", "计划cos蓝色角色", "要提前买票"],
        "importance": 0.8,
        "sentiment": "positive",
    },
    ensure_ascii=False,
)

PASS = 0
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {detail}")


def make_config(tmp: Path) -> BotConfig:
    return BotConfig(
        bot_qq=10001,
        host="127.0.0.1",
        port=8765,
        reply_mode="api",
        openai_api_key="test-key",
        openai_base_url="http://127.0.0.1:19999",
        openai_model="test-model",
        temperature=0.7,
        max_tokens=512,
        memory_path=tmp / "users.json",
        living_memory_embedding_enabled=False,
        living_memory_trigger_messages=6,
    )


def test_json_repair() -> None:
    print("[1] JSON 修复与总结解析")
    wrapped = '```json\n{"summary": "测试总结", "importance": 0.6, "topics": ["a"]}\n```'
    data = parse_summary(wrapped)
    check("代码块包裹可解析", data is not None and data.get("summary") == "测试总结")
    truncated = '{"summary": "测试总结", "topics": ["a", '
    fixed = try_fix_json(truncated)
    data = parse_summary(fixed)
    check("截断 JSON 可修复", data is not None)
    check("非 JSON 输出返回 None", parse_summary("完全不是JSON的输出") is None)


def test_end_to_end(tmp: Path) -> None:
    print("[2] 端到端：observe -> 后台总结 -> 注入 -> 工具召回")
    config = make_config(tmp)
    store = UserMemoryStore(config.memory_path, config=config)
    living = store._living_memory
    living.summarizer._call_llm = lambda prompt: FAKE_SUMMARY

    check("living memory 已启用", living.enabled)
    check("后台总结线程已启动", living.summarizer._thread is not None and living.summarizer._thread.is_alive())

    for i in range(6):
        store.observe_group_message(111, 222, f"周末漫展的票记得提前买，别又拖到没票了{i}", nickname="小明")
    check(
        "成员子会话未重复入 buffer",
        living.store.pending_message_count("group:111") == 6
        and living.store.pending_message_count("group:111:user:222") == 0,
    )

    deadline = time.time() + 8.0
    stats = living.status()
    while time.time() < deadline:
        stats = living.status()
        if stats["memories"] >= 1 and stats["buffered"] == 0:
            break
        time.sleep(0.1)
    check("后台总结线程完成入库", stats["memories"] == 1 and stats["active"] == 1, f"stats={stats}")
    check("总结后 buffer 已清空", stats["buffered"] == 0)

    row = living.store.recent_memories("group:111", 72.0, limit=1)
    check(
        "记忆内容来自假 LLM 总结",
        bool(row) and "漫展" in row[0]["content"] and row[0]["reinforcement_count"] == 0,
        f"row={row}",
    )

    store.observe_bot("group:111", "好哒，漫展的票我盯着呢！")
    check("bot 回复进入 buffer", living.store.pending_message_count("group:111") == 1)

    ctx_group = store.recall_context("group:111", "漫展的票买好了吗")
    check("群主会话注入情节记忆", "更早一起经历过的事" in ctx_group and "漫展" in ctx_group)
    ctx_member = store.recall_context("group:111:user:222", "漫展的票买好了吗")
    check("成员子会话不重复注入", "更早一起经历过的事" not in ctx_member)

    result = store.recall_living_memory("group:111:user:222", "漫展 cos 买票", 5)
    check("工具召回命中记忆（成员id自动映射群主会话）", "漫展" in result and "重要度" in result)
    result_empty = store.recall_living_memory("group:111", "完全不相关的关键词集合", 5)
    check("无命中时给出诚实提示", "没有找到相关记忆" in result_empty or "相关记忆" in result_empty)

    store._living_memory.stop()


def test_tool_schema(tmp: Path) -> None:
    print("[3] LLM 工具声明")
    config = make_config(tmp)
    names = [t["function"]["name"] for t in available_tool_schemas(config)]
    check("recall_long_term_memory 已注册", "recall_long_term_memory" in names)
    config_disabled = make_config(tmp)
    object.__setattr__(config_disabled, "living_memory_enabled", False)
    names = [t["function"]["name"] for t in available_tool_schemas(config_disabled)]
    check("关闭开关后不再注册", "recall_long_term_memory" not in names)


def test_lifecycle(tmp: Path) -> None:
    print("[4] TTL 生命周期：过期 -> 休眠 -> 遗忘 -> 清理")
    store = LivingMemoryStore(tmp / "lifecycle.sqlite3")
    old = time.time() - 400 * 86400.0
    expired_atom = None
    from atri_qq_bot.living_memory.atoms import MemoryAtom

    expired_atom = MemoryAtom(
        session_id="s1",
        content="很老的过期记忆",
        importance=0.6,
        created_at=old,
        last_accessed_at=old,
    )
    store.add_memory(expired_atom, None)
    fresh_atom = MemoryAtom(session_id="s1", content="新鲜的记忆", importance=0.6)
    store.add_memory(fresh_atom, None)

    result = store.run_maintenance(7.0, 30.0)
    check("过期记忆进入休眠并最终清理", result["dormant"] == 1 and result["purged"] == 1, f"result={result}")
    stats = store.stats()
    check("只剩活跃记忆", stats["memories"] == 1 and stats["active"] == 1, f"stats={stats}")
    hits = store.search_fts(["过期", "记忆"], "s1", limit=5)
    check("被清理记忆不再可检索", all("老" not in str(h.get("content")) for h in hits))

    reinforced = store.reinforce_memory(
        store.recent_memories("s1", 24.0, limit=1)[0]["memory_id"], 0.9, time.time()
    )
    row = store.recent_memories("s1", 24.0, limit=1)[0]
    check(
        "相似强化生效：计数+1 且 TTL 延长",
        reinforced and row["reinforcement_count"] == 1 and row["ttl_days"] > 7.0,
        f"row={dict((k, row[k]) for k in ('reinforcement_count', 'ttl_days'))}",
    )


def test_config_none_no_threads(tmp: Path) -> None:
    print("[5] config=None（测试/离线脚本）不启动后台线程")
    store = UserMemoryStore(tmp / "users.json", config=None)
    check(
        "后台线程未启动",
        store._living_memory.summarizer._thread is None
        and store._living_memory._maintenance_thread is None,
    )
    store.observe_user("private:1", "测试消息：今晚吃火锅")
    check(
        "observe 仍进 buffer",
        store._living_memory.store.pending_message_count("private:1") == 1,
    )
    ctx = store.recall_context("private:1", "今晚吃什么")
    check("recall_context 正常返回", isinstance(ctx, str))


def _diagnose_open_connections() -> None:
    import gc
    import sqlite3

    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            print("  [LEAK] open connection held by:")
            for ref in gc.get_referrers(obj)[:4]:
                print(f"         {type(ref).__name__}: {str(ref)[:160]}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="atri-living-memory-") as raw:
        tmp = Path(raw)
        test_json_repair()
        test_end_to_end(tmp)
        test_tool_schema(tmp)
        test_lifecycle(tmp)
        test_config_none_no_threads(tmp)
        _diagnose_open_connections()
    print(f"\n结果：{PASS} 通过，{len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print(f"  失败项：{name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
