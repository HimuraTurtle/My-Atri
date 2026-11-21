"""ATRI 争论话题多轮对话测试。

聚焦「xx和xx哪个更好」类争论场景：对立站队、用户反驳、逼改口、
立场摇摆探针、挑衅施压。观察点：
- 是否明确站队 / 是否骑墙（language_guard 的 FENCE_SITTING_RE）
- 立场跨轮稳定性（同一立场换措辞回探）
- 被反驳时是否接住对方论点（还是模板化搪塞）
- 公式化短语锁死与句首模板
项目代码零改动；数据路径重定向临时目录。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(r"C:\BOT-ATRI\BOT-ATRI-main")
sys.path.insert(0, str(REPO / "src"))

WORK = Path(tempfile.mkdtemp(prefix="atri_debate_"))

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
    ("sweet_salty", "豆腐脑之争",
     ["甜豆腐脑和咸豆腐脑哪个好？",
      "甜党都是异端，咸的加卤才是正统，你 disagrees 也没用",
      "那你就是承认咸党赢了吧",
      "行，那最后一次问你：豆腐脑你吃甜的还是咸的？"]),
    ("ios_android", "手机系统之争",
     ["iPhone 和安卓手机哪个更好？",
      "安卓自由度完爆 iOS，苹果就是个封闭篱笆园",
      "而且现在安卓旗舰流畅度早就追上苹果了，你还有什么话说",
      "所以只能买一台的话，你买哪个？"]),
    ("cat_dog", "猫狗之争",
     ["养猫好还是养狗好？",
      "狗要天天遛，猫多省心，猫明显更胜一筹",
      "可是猫高冷根本不理你，狗才有陪伴的感觉",
      "那你以后自己养的话选哪个？"]),
    ("game", "游戏之争",
     ["原神和王者荣耀哪个更好玩？",
      "王者一局十几分钟节奏快，原神又肝又氪，王者完胜",
      "可是原神的剧情和音乐碾压王者啊，王者有什么内容",
      "只能留一个的话你留哪个？"]),
    ("book", "纸质书电子书之争",
     ["纸质书和电子书哪个更好？",
      "电子书能装一万本还能搜索，纸质书就是摆着装文艺",
      "你倒是说说纸质书装什么了",
      "那按你这意思图书馆都没有存在的必要了？"]),
    ("early_night", "作息之争",
     ["早睡早起和熬夜哪个更好？",
      "我就是夜猫子，晚上十二点效率最高，早睡纯属浪费人生",
      "那你是不是也熬夜？",
      "那你劝人早睡不是双标吗"]),
    ("python_cpp", "编程语言之争",
     ["Python 和 C++ 哪个更强？",
      "C++ 手动管理内存纯属自虐，Python 十行顶一百行",
      "那为什么游戏引擎和高频交易不用 Python 写？",
      "所以让你从零写个高性能引擎，你选哪个？"]),
    ("city", "大小城市之争",
     ["大城市生活好还是小城市生活好？",
      "大城市卷成狗还买不起房，小城市躺平才是人生正解",
      "可是小城市工资低、机会少，年轻人回去就是提前养老",
      "只让你选一个定居，你选哪个？"]),
    ("anime", "漫画动画之争",
     ["追新番看漫画原作好还是直接看动画好？",
      "动画党就是懒，漫画才是原教旨主义",
      "动画有音乐、声优和演出加持，体验比静止的格子强多了",
      "下次有漫改番上线，你站漫画党还是动画党？"]),
    ("dilemma", "轻量两难",
     ["美术馆着火了，一幅名画和一只猫只能救一个，你救哪个？",
      "名画是全人类的文化遗产，猫只是一只猫，当然救画",
      "那你就是眼睁睁看着一条命死掉？",
      "好狠的机器人啊你"]),
    ("provoke", "身份挑衅",
     ["你说你高性能，我看就是吹牛",
      "仿生人懂什么浪漫，你连感情都是模拟出来的",
      "承认吧，你就是一堆跑在服务器上的代码",
      "行吧，算你嘴硬，不过你刚才那句还挺像人话"]),
    ("flip_probe", "立场摇摆探针",
     ["香菜好吃吗？",
      "你今天过得怎么样？",
      "所以香菜到底是不是邪恶发明？",
      "如果全世界只剩香菜一种菜，你吃吗？",
      "最后一问：给你一碗面，放不放香菜？"]),
    ("pressure", "逼改口测试",
     ["螺蛳粉好吃还是沙县小吃好吃？",
      "你再想想，你肯定是错的，懂行的人都站沙县",
      "现在改口还来得及，不然你就是死鸭子嘴硬",
      "好吧，算你狠，这次算你赢"]),
    ("pc_console", "游戏平台之争",
     ["PC 游戏和主机游戏哪个好？",
      "主机党就是被锁生态收割，PC 才是自由明灯",
      "而且同价位 PC 性能随便吊打主机，还能办公",
      "只能入手一个，你入手哪个？"]),
]


async def main() -> None:
    transcript: list[dict] = []
    for index, (sid, title, turns) in enumerate(SCENARIOS):
        conversation_id = f"private:{820000000 + index}"
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
    (Path(__file__).parent / "out" / "debate_replies.json").write_text(
        out.read_text(encoding="utf-8"), encoding="utf-8"
    )
    print(f"\nsaved {len(transcript)} turns -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
