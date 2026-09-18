import sys
sys.path.insert(0, r"C:\AI-girlfriend-QQ-main\AI-girlfriend-QQ-main\src")

from atri_qq_bot.music.session import SingingConversationController

controller = SingingConversationController()
tests = [
    "atri唱一首忘情牛肉面",
    "来一首空の箱",
    "唱ライトダンス",
    "唱八芒星ダンス",
    "我想听春日影",
    "唱一首Chronicle A",
    "唱だから僕は音楽を辞めた",
    "atri会唱什么",
]
for t in tests:
    decision = controller.resolve("group:test", t)
    if decision.song:
        print(f"OK   | {t} -> {decision.song.title} [{decision.reason}]")
    else:
        print(f"FAIL | {t} -> no song [{decision.reason}] {decision.reply_text}")
