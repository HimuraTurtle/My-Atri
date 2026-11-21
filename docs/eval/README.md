# 对话质量评估语料

多轮对话测试的基线语料，配合 `tools/eval/` 脚本做 AIGC/公式化回答分析。

## 文件

- `chat_baseline.json` — 2026-11-18 闲聊基线：12 场景 × 5 轮（日常/情绪/站队/求助/纠错/原作/夸奖/梗/追问/问候/吐槽/试探），当时确认的公式化问题见语料内回复（会话内短语锁死、句首模板、答非所问）。
- `debate_baseline.json` — 2026-11-18 争论基线：14 场景 × 4~5 轮（对立站队/逼改口/立场探针/挑衅），评估口径见 atri-eval-criteria：像人 + 立场坚定 + 情绪化，骑墙话为唯一硬违规。

## 复跑

```powershell
.\.venv\Scripts\python.exe -I tools\eval\run_debate_test.py      # 产出 out/debate_replies.json
.\.venv\Scripts\python.exe -I tools\eval\analyze_debate.py       # 与基线同口径分析
```

评估口径变更时先更新基线，再做提示词/情绪档改动的前后对比（参考 d1d95ff 的四样本对比法）。
