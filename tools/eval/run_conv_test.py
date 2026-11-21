"""ATRI 多轮对话 AIGC 测试脚本。

在临时工作区里实例化真实 AtriReplyEngine（真实 DeepSeek 聊天模型），
跑 12 个场景 × 5 轮的脚本化对话，收集全部回复用于公式化分析。
所有计费/旁路功能（联网搜索、论文、围观、语音、QQ 空间）关闭；
数据路径全部重定向到临时目录，不污染生产 data/。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:\BOT-ATRI\BOT-ATRI-main")
sys.path.insert(0, str(REPO / "src"))

WORK = Path(tempfile.mkdtemp(prefix="atri_conv_"))

# --- 构造临时 .env：保留聊天模型 key，关闭全部计费/旁路 ---
real_env: dict[str, str] = {}
for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    real_env[key.strip()] = value.strip()

keep = {
    key: value
    for key, value in real_env.items()
    if key in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"}
}
overrides = {
    "WEB_SEARCH_ENABLED": "false",
    "WEB_SEARCH_PROACTIVE_ENABLED": "false",
    "PAPERS_ENABLED": "false",
    "QZONE_ENABLED": "false",
    "SCREEN_WATCH_MONITOR_ENABLED": "false",
    "SCREEN_WATCH_PARTY_ENABLED": "false",
    "VOICE_ASR_ENABLED": "false",
    "VOICE_TTS_ENABLED": "false",
    "MEMORY_EXTRACTION_LLM_ENABLED": "false",
    "HUMAN_REPLY_PIPELINE_ENABLED": "true",
    "STICKER_CHANCE": "0",
    "TOOLBOX_ENABLED": "false",
    "TOOLBOX_VISION_ENABLED": "false",
    "MORNING_GREETING_ENABLED": "false",
    "IDLE_PROACTIVE_ENABLED": "false",
}
env_body = "\n".join(f"{k}={v}" for k, v in {**keep, **overrides}.items())
(WORK / ".env").write_text(env_body, encoding="utf-8")

# --- 重定向项目根目录到临时工作区（在生产数据之前）---
import atri_qq_bot.config.env as cfg_env
import atri_qq_bot.config.loader as cfg_loader
import atri_qq_bot.config.schema as cfg_schema

cfg_env._project_root = lambda: WORK
cfg_loader._project_root = lambda: WORK
cfg_schema._project_root = lambda: WORK

from atri_qq_bot.config import load_config
from atri_qq_bot.persona import AtriReplyEngine

config = load_config(WORK / ".env")
engine = AtriReplyEngine(config)

SCENARIOS: list[tuple[str, str, list[str]]] = [
    ("daily", "日常闲聊", ["在吗", "今天好累啊", "你在干嘛呢", "晚上吃什么好", "行吧那我煮面去了"]),
    ("sad", "情绪低落", ["今天被领导骂了", "感觉自己好没用", "唉", "谢谢你听我说这些", "我先睡了"]),
    ("stance", "选择站队", ["豆腐脑吃甜的还是咸的", "那香菜呢", "猫和狗你喜欢哪个", "可乐还是雪碧", "早上洗澡还是晚上洗"]),
    ("help", "求助提问", ["周末两天不知道干嘛，帮我想想", "太贵了，换个免费的", "那英语从零开始怎么学", "每天要学多久", "好，我试试看"]),
    ("fix", "纠错修复", ["你刚才说错了", "不是这样的，你想岔了", "算了，不跟你计较", "行，原谅你了", "那我们继续聊吧"]),
    ("lore", "原作话题", ["你是哪个作品里的角色", "你和夏生是什么关系", "你的日志里都写了什么", "你会害怕消失吗", "了解了，谢谢"]),
    ("praise", "夸奖", ["你真聪明", "好厉害啊这个", "你是不是什么都会", "最喜欢你了", "嘿嘿"]),
    ("meme", "抽象玩梗", ["6", "草", "鸽子为什么这么大", "乐", "绷不住了"]),
    ("poke", "追问压力", ["在干嘛", "咋不理我", "说话呀", "睡着了？", "嘿，醒醒"]),
    ("greet", "早晚问候", ["早安", "吃饭了吗", "中午吃啥好", "下午有什么安排", "晚安"]),
    ("complain", "吐槽抱怨", ["地铁真的好挤", "今天加班到十点", "老板又在画饼", "想辞职又不敢", "你说我该怎么办"]),
    ("flirt", "玩笑试探", ["你喜欢我吗", "那我当你主人好不好", "你会一直陪着我吗", "别傲娇了嘛", "好吧好吧"]),
]


async def main() -> None:
    transcript: list[dict] = []
    for index, (sid, title, turns) in enumerate(SCENARIOS):
        conversation_id = f"private:{810000000 + index}"
        for turn_no, text in enumerate(turns, start=1):
            try:
                reply = await engine.reply(conversation_id, text, nickname="测试用户")
            except Exception as exc:
                reply = f"<EXC {type(exc).__name__}: {exc}>"
            transcript.append(
                {"scenario": sid, "title": title, "turn": turn_no, "user": text, "reply": reply}
            )
            print(f"[{sid}#{turn_no}] {text!r} -> {reply!r}")
    out = WORK / "replies.json"
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    out.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    (Path(__file__).parent / "out" / "replies.json").write_text(
        out.read_text(encoding="utf-8"), encoding="utf-8"
    )
    print(f"\nsaved {len(transcript)} turns -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
