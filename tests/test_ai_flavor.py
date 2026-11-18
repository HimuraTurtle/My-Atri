import asyncio
from pathlib import Path
from typing import Any

from atri_qq_bot.config import BotConfig
from atri_qq_bot.language_guard import detect_ai_flavor, extract_phrase_fingerprints
from atri_qq_bot.persona import AtriReplyEngine


def _config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        bot_qq=100000001,
        host="127.0.0.1",
        port=8765,
        reply_mode="smart",
        openai_api_key="ollama",
        openai_base_url="http://127.0.0.1:11434/v1",
        openai_model="qwen3:4b-instruct",
        temperature=0.60,
        max_tokens=180,
        frequency_penalty=0.35,
        chat_quality_retries=2,
        memory_path=tmp_path / "users.json",
    )


def test_detect_ai_flavor_flags_high_frequency_catchphrases() -> None:
    for reply in (
        "我记下了",
        "行吧，我记下来了",
        "记住了记住了，下次不会了",
        "别急，我听着呢",
        "没事，我在这儿呢",
        "嘿嘿，被夸了",
        "图发来，我帮你认认",
        "错我认，锅我不背",
        "那当然，我可是高性能的",
    ):
        assert detect_ai_flavor(reply), reply


def test_detect_ai_flavor_allows_sharp_opinions() -> None:
    for reply in (
        "香菜就是邪恶发明，谁放香菜我跟谁急",
        "选甜的，这还用问？咸豆浆是异端",
        "螃蟹，永远是螃蟹",
        "那必须是我，跑得快算得准",
        "不知道诶，这个我真没见过",
    ):
        assert detect_ai_flavor(reply) is None, reply


def test_detect_ai_flavor_flags_fence_sitting() -> None:
    for reply in (
        "各有千秋吧",
        "萝卜青菜，各有所爱",
        "这个因人而异",
        "看你个人喜好咯",
        "两个都不错，看你自己",
        "见仁见智啦",
    ):
        assert detect_ai_flavor(reply), reply


def test_extract_phrase_fingerprints_skips_short_lines() -> None:
    reply = "嗯\n这表情包也太离谱了吧哈哈哈\n好"
    fingerprints = extract_phrase_fingerprints(reply)
    assert fingerprints == [("这表情包也太", "谱了吧哈哈哈")]


def _guarded_engine(tmp_path: Path, replies: list[str]) -> tuple[AtriReplyEngine, list[dict[str, Any]]]:
    class GuardedEngine(AtriReplyEngine):
        def __init__(self, config: BotConfig) -> None:
            super().__init__(config)
            self.calls: list[dict[str, Any]] = []

        async def _reply_with_api(self, *args, **kwargs) -> str:  # type: ignore[no-untyped-def]
            self.calls.append(kwargs)
            return replies[min(len(self.calls) - 1, len(replies) - 1)]

    engine = GuardedEngine(_config(tmp_path))
    return engine, engine.calls


def test_guarded_api_retries_ai_flavor_until_clean(tmp_path: Path) -> None:
    engine, calls = _guarded_engine(
        tmp_path, ["好，我记下了", "记住了记住了", "这就去办，马上给你结果"]
    )

    reply = asyncio.run(
        engine._reply_with_guarded_api("private:1", "帮我记一下", "测试")
    )

    assert reply == "这就去办，马上给你结果"
    assert len(calls) == 3
    # 第二、三次调用应注入表达重试提示与违规表达说明
    assert "违规表达" in (calls[1]["extra_system"] or "")
    assert "我记下了" in (calls[1]["extra_system"] or "")
    # 首次调用不应携带重试提示
    assert calls[0]["extra_system"] is None


def test_guarded_api_returns_clean_reply_when_flavor_retries_exhausted(
    tmp_path: Path,
) -> None:
    # AI 味是软违规：重试耗尽后返回最后一条语言合格回复，而不是抛错。
    engine, calls = _guarded_engine(tmp_path, ["行，我记下了"] * 4)

    reply = asyncio.run(
        engine._reply_with_guarded_api("private:1", "帮我记一下", "测试")
    )

    assert reply == "行，我记下了"
    assert len(calls) == 3  # chat_quality_retries=2 → 三次尝试


def test_guarded_api_still_hard_fails_on_garbage(tmp_path: Path) -> None:
    engine, calls = _guarded_engine(tmp_path, ["كيفية تقييم هذا الملف؟"] * 4)

    try:
        asyncio.run(
            engine._reply_with_guarded_api("private:1", "随便聊聊", "测试")
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_phrase_ledger_injects_banned_hint(tmp_path: Path) -> None:
    engine, calls = _guarded_engine(tmp_path, ["收到，马上安排给你结果"])
    engine._record_phrase_ledger("private:2", "哼，我是高性能的嘛")

    asyncio.run(
        engine._reply_with_guarded_api("private:2", "在吗", "测试")
    )

    hint = calls[0]["extra_system"] or ""
    assert "禁用" in hint
    assert "是高性能的嘛" in hint


def test_phrase_ledger_rejects_repeated_tail(tmp_path: Path) -> None:
    engine, calls = _guarded_engine(
        tmp_path, ["哼，我可是高性能的嘛", "下次换个说法给你看"]
    )
    engine._record_phrase_ledger("private:3", "上一次也是我可是高性能的嘛")
    reply = asyncio.run(
        engine._reply_with_guarded_api("private:3", "夸夸你", "测试")
    )

    assert reply == "下次换个说法给你看"
    assert len(calls) == 2
    assert "收尾" in (calls[1]["extra_system"] or "")
