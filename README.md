# 亚托莉 QQ 陪伴机器人 — Atri QQ Bot

<p align="center">
  <strong>基于 OneBot/NapCat 协议的 AI 陪伴机器人</strong><br>
  RAG 记忆检索 · Function Calling · 联网搜索/论文检索 · 语音/歌声 · 屏幕看护围观 · 工具箱 · 桌宠 · Web 管理面板
</p>

---

## 仓库结构

- `src/atri_qq_bot/` — 机器人主服务：OneBot 接入、人格与记忆、认知内核、工具箱与各能力模块
- `src/atri_webui/` — 本机管理面板（WebUI）与 HTTP API
- `src/atri_desktop/` — Windows 桌宠（托盘图标与启动菜单）
- `src/atri_qq_bot/qzone/` — QQ 空间模块（说说/点赞/评论与开发日志闭环）
- `tests/` — 全量回归测试（46 个测试文件）
- `docs/prompts/` — 运行时提示词，由代码 `load_prompt` 加载，改名需同步代码
- `docs/learning/` — 项目讲解与课程材料
- `docs/research/` — 原作考据与人设档案
- `docs/prototypes/` — 界面原型与宣传图
- `docs/`（根）— 架构总览、交接文档与模块专题说明（评测见 `insights.md`）
- `tools/` — 启动链、桌宠、语音管线、记忆索引维护与环境检查脚本
- `deploy/` — 云端部署说明与 NapCat OneBot v11 配置样例
- `data/` — 表情包素材、触发词与运行数据（密钥不入库）
- `logs/` — 运行日志（git 不跟踪）

根目录的中文 `.bat` 是给使用者的双击入口，实际逻辑都在 `tools/` 内：`安装亚托莉.bat` 运行 `tools/install/install.ps1` 五阶段安装向导，`停止亚托莉.bat` 调 `tools/launch/watchdog.ps1` 停止核心与守卫，`检查亚托莉状态.bat` 调 `tools/check-status.ps1` 做只读运行状态检查。

开发者调试、服务关系与本机端口见 [DEVELOPER.md](DEVELOPER.md)。

## 版本与发布状态

| 分支 | 版本 | 状态与定位 |
|------|------|------------|
| `main` | **v0.5** | 当前默认分支，跟随最新稳定代码 |
| `v0.4` | **v0.4** | 上一代发行版本，包含认知内核、评测、群聊总结与 QQ 空间扩展 |
| `v0.3` | **v0.3** | 再上一代稳定版本：RAG 记忆、Agent 工具、语音/歌声与 WebUI |
| `v0.2` | **v0.2** | 包结构重构与 LLM Function Calling 工具系统 |
| `v0.1-original` | **v0.1** | 原始扁平文件版本，仅作历史存档 |

## v0.5 新增功能

v0.5 在 v0.4 的基础上新增七条能力线：学术检索工具、屏幕看护从“快照问答”升级为“持续围观陪伴”、针对回复错乱/张冠李戴的可靠性修复、QQ 空间重构为会话复用版、每轮对话主动联网检索、图片回复改为“默认自然接话、被问才具体描述”，以及语言风格从“规范书面输出”改为“模仿群友节奏、句式灵活多变”。所有新增功能沿用既有降级契约：失败时坦诚说明，绝不编造结果。

### 1. 学术论文检索（`search_papers`）

- 新增 `papers/` 模块与 `search_papers` 工具：接入 OpenAlex 全学科文献库（免 key），覆盖期刊论文与预印本，返回标题、作者、年份、期刊、被引数、DOI 链接与按倒排索引重建的摘要。
- **排序契约**：OpenAlex 的 `sort` 会覆盖相关度排序、容易返回“全网高被引但跑题”的文献，因此请求永远先按相关度取候选池，`cited`（找经典）/`newest`（找最新进展）只在池内重排。
- 用户找论文、文献、研究进展或写论文查资料时由聊天模型自主调用，学术类需求优先于 `search_web`；结果必须附原文 url，查不到时坦诚说明，严禁编造标题、作者或链接。

### 2. 屏幕看护围观模式（`screen_watch`）

- **屏幕看护基线**：主人问“我现在在干嘛”等指令时自动截屏，用视觉模型生成画面摘要回复；敏感窗口（密码/支付/隐私类标题）自动打闸，不看也不发。
- **围观模式**：主人说“看着我写论文/打怪”后，ATRI 持续看屏并插短评——帧差分确认画面显著变化（连续两帧）才评论，且两次评论至少间隔 3 分钟；也支持在识别到主人的活动宣告后主动开启围观。
- 结束由主人话语触发（宽松规则，宁可误停不刷屏）；截图/视觉连续失败达到上限自动收工并坦诚告知。
- 围观话术全部由主聊天模型结合人格与记忆动态生成（无硬编码模板），称呼按会话类型与账号档案走，任何场景禁用“主人”一词；LLM 失败退回不含称呼的简短兜底。

### 3. 回复错乱修复：任务账本 + 自洽审查 + 轮次观测

针对“把已经做过的事说成还没做/待会儿再做”的时序错乱，落地三层防线：

- **任务状态账本（方案一）**：成功的工具回执与确定性命令（B 站视频总结、漫画阅读/下载/搜索/章节等）写入会话级账本；注入上下文时带 `[刚刚]`/`[X分钟前]` 相对时间标签，让模型分清“刚做过什么”与“还没做什么”。
- **自洽审查防线（方案二）**：纯规则检测候选回复里的“声称待办”话术与账本事实的矛盾——同族动词互认（查/搜/检/找/总结/解析…）避免用词不一致漏判，话题双向提取覆盖“瑞士轮的对阵我这就去搜”（话题前置）与“这就去搜瑞士轮”（话题后置）两类句式。命中后注入账本事实重写一次；查无实据不拦、重写失败不丢回复，判定零额外成本。
- **轮次观测日志（方案三）**：`reply-events.jsonl` 在原有事件维度上补全本轮工具回执、账本矛盾审查结果与账本快照，让错乱可定位、改进可验证；观测数据每轮消费后即弃。

### 4. 群记忆归属修复

- 群主会话的结构化记忆（L1/L2/L3）入库时记录发言群友 `actor_name`；记忆渲染与语义检索两条通道都按“群友“昵称””标注主语，旧条目无归属字段时降级为“有位群友”，彻底消除把群友 A 的行为安到群友 B 头上的现象。
- 群聊当前说话者提示同步强化：明确“上下文里其他群友的发言、行为和记忆都属于各自本人，绝对不要安到当前说话的这位群友头上”。

### 5. QQ 空间（会话复用版）

- **会话复用**：不独立登录、不常驻保活，Cookie 经 OneBot `get_cookies(domain="user.qzone.qq.com")` 从 NapCat 按需借取，进程内仅 TTL 缓存，流量只在实际使用时产生；发布/点赞/评论请求失效自愈重试一次。
- **主人口令**：「发条说说」[可选正文] 是发说说的唯一入口——带正文直发原文，不带正文按当日主题轮换由模型生成，生成稿必须通过质量闸（反编造、反套话、排重、称呼禁令）才发布；「更新日志」触发开发手记闭环（采集新提交 → 生成手记 → 发布）。
- **自动评论保留**：每日锚点时间 ± 随机抖动执行一轮，跳过自己与已评论动态；聊天中另有小概率（默认 0.0005）随机评论一条好友动态，带冷却防连发。
- **自然语言工具**：`qzone_view_recent_feeds` / `qzone_view_user_feeds` / `qzone_like_feed` / `qzone_comment_feed`，点赞与评论仅主人私聊可用，重复动作由动作日志幂等拦截。
- **零迁移升级**：数据沿用 `data/qzone_automation.sqlite3`，开发手记水位无缝继承；旧 `qzone_admin` / `qzone_automation` / `plugins/qqzone` 链路与 WebUI「QQ动态」面板全部移除。

### 6. 主动联网检索（每轮自动执行）

针对“ATRI 需要『你搜一下』这类提示词才肯联网搜索、模型自发调用工具意愿不稳定”的问题，把联网搜索从模型决策改为系统侧确定性执行：

- **每轮必搜**：回复生成前由系统自动执行一次联网搜索，结果作为系统消息注入上下文（位于工具合同与真实时间块之后）；模型只负责参考或忽略，不再承担“要不要搜”的决策责任，需要更具体的信息时仍可按需调用 `search_web`。
- **门槛过滤**：纯问候/纯语气消息（“早上好”“嗯嗯”“破防了”等 30+ 词表）自动跳过，不浪费搜索配额。
- **时敏消息自动切 news 模式**：消息含“最近/最新/今天/现在/近况/新闻/时事/今年”等标记时走 news 模式，博查按时间敏感度分级加时效过滤（今天→1 天、最近/近况/新闻→1 周、今年/去年→1 年、news 默认 1 月、普通网页查询不加过滤），RSS 兜底也优先新闻源，避免注入陈旧资料后模型说“信息滞后”。
- **检索词清洗**：剥掉口语引导（“你知道”“帮我查一下”）与疑问词（“是谁”“有哪些”“怎么样”），只留话题实体，减少搜索噪音。
- **轮内备忘**：质量重试的所有温度档共享同一份证据，同一轮只搜一次；同一会话 10 分钟内相同检索词命中缓存不重复计费。
- **可观测日志**：每轮主动检索打 info 日志（跳过原因/完成时 query 与 mode/无可用结果说明），排查“到底搜没搜”不用再考古。
- **提示词约束**：明确告知模型资料“系统已主动执行”——不重复检索已注入内容、不把无关结果硬扯进闲聊、不向用户描述“我去搜索了”的过程。
- **降级契约**：检索任何环节失败仅记 warning 日志并静默跳过，绝不阻断正常回复；博查按次计费，高频群聊可用 `WEB_SEARCH_PROACTIVE_ENABLED=false` 一键关闭。

### 7. 图片回复行为调整（默认自然接话，被问才描述）

针对“用户只是分享图片，ATRI 却每次都复述一遍画面内容”的问题，调整视觉证据的注入提示词：

- **默认自然接话**：视觉结果定位为“看过图后的背景印象”——像朋友看完图那样自然接话，不主动复述或描述画面内容，也不把画面细节当成必须提到的任务。
- **被问才具体描述**：只有用户问“图里有什么”“画的是谁”“写了什么字”等具体内容时，才展开具体描述。
- **防幻觉约束不变**：人物身份、画中文字和画面细节仍只能来自本轮视觉结果或本轮联网核验；被问时必须直接回答，不得反问用户想让你识别还是描述。

### 8. 语言风格调整（模仿群友节奏，句式灵活多变）

针对“回复语言略显生硬、像被文本蒸馏过的规范书面语”的问题，排查发现根源是三份提示词叠加的风格规范化（“短而完整”“保持完整但简洁”）把输出压成了主谓宾齐全、句号收尾的工整句式，与真实 QQ 群的碎片化口语形成反差。调整三份运行时提示词：

- **群聊规则**（`group_chat.md`）：新增“说话节奏向群友看齐”——观察上下文里群友最近的句长、语气和用梗习惯，像群里的一员那样接话，群友随意就随意、群里玩梗就顺着玩；“句式灵活多变”——长短交错、允许口语碎片（“哈哈哈确实”“啊这”“没毛病”）、允许结尾不带句号，不套固定句式模板。同时明确模仿的是节奏和松弛感，保持 ATRI 自己的口癖与身份底色。
- **人格核心**（`atri_persona.md`）：语言手感放松为“默认简短，闲聊接话可以只有半句”；新增总原则“句式和节奏跟着对方与场景走：对方正经提问就答得完整清楚，对方闲聊斗图就短句接梗”。
- **回复运行契约**（`reply_contract.md`）：群聊风格改为“像群友那样自然接话，句式长短随话题和现场节奏，不追求工整”。
- 防幻觉、不编造、不泄露私聊记忆等红线约束全部保留，仅调整风格指令。

新增配套单测：论文检索 22 项、围观模式 28 项、回复错乱 29 项、记忆归属 6 项、QQ 空间 61 项、主动检索 23 项（含时敏模式选择与博查时效分级）；全量回归 894 项通过（图片回复与语言风格为提示词调整，未新增用例数）。

## v0.4 技术框架与版本变化

v0.4 在 v0.3 的聊天、RAG、工具调用和语音能力上，增加了“状态连续性—回复质量—外围能力”三条旁路，并把原先集中在少数大文件中的逻辑拆成可测试模块。核心原则是：实时回复链路优先可用，认知、评测、总结和自动化失败时可以独立降级。

### 技术栈总览

v0.4 的技术栈按职责分层如下：

- **运行时**：Python 3.11、`asyncio`、`python-dotenv`；负责异步任务、配置加载和后台服务生命周期。
- **QQ 接入**：NapCat / OneBot v11、`websockets` 14、反向 WebSocket；负责事件接收、会话排队、消息发送和健康检查。
- **应用编排**：`ConversationTurn`、Protocol collaborators、`TurnTrace`；固定“上下文 → Agent → 交付计划”的单轮事务边界。
- **回复引擎**：`AtriReplyEngine`、OpenAI-compatible API、Tool Registry；负责人格提示词、回复重写、Function Calling 和失败降级。
- **记忆检索**：JSON 事实源、SQLite FTS5、可选 Ollama `bge-m3`；负责分层记忆、语义召回、原作设定和用户上下文压缩。
- **认知内核**：LangGraph `StateGraph`、`CognitionKernel`、SQLite/WAL；负责理解、能力提案和状态演化，当前以 shadow mode 安全运行。
- **质量旁路**：`InsightsRuntime`、SQLite、LLM evaluator、`jieba`、可选 `sentence-transformers`；负责回复评测、群消息归档、主题聚类和周期报表。
- **Web 管理**：Python `ThreadingHTTPServer` + `asyncio`、模块化 `page/`；提供配置、记忆、模型、评测、群总结、音乐和语音页面。
- **语音与媒体**：本机 HTTP voice service、GPT-SoVITS、Edge-TTS、SenseVoice、Pillow、RapidOCR、pypdf、ffmpeg；支持 ASR/TTS、歌声工程、图片/OCR/PDF/视频处理。
- **资源治理**：跨进程文件锁、优先级队列、Ollama 按需启动/卸载；协调聊天、用户媒体、视觉和后台任务对 GPU/内存的竞争。

### v0.4 请求链路

```text
NapCat / OneBot
      │ 反向 WebSocket
      ▼
OneBotServer ── 会话队列 / 防抖 / 健康检查
      ▼
ConversationTurn
      ├─ ContextAssembler：记忆检索、视觉/文档/语音材料、人格上下文
      ├─ AgentRunner：AtriReplyEngine、LLM Tool Registry、工具回执
      └─ DeliveryPlanner：文本拆分、语音/图片/音乐动作、OneBot 投递
             │
             ├── CognitionKernel（LangGraph shadow state）
             └── InsightsRuntime（异步归档、评测、群聊总结）
```

### 相比 v0.3 的新增功能与技术优化

以下采用纵向说明，避免在窄屏、移动端或 GitHub 代码视图中出现宽表格裁切。

#### 1. 持续状态

- **v0.3**：主要依赖人格提示词、短期上下文和三层用户记忆。
- **v0.4**：新增 `src/atri_qq_bot/cognition/`。`CognitionKernel` 使用 LangGraph `StateGraph` 连接“理解”和“能力提案”节点，再由 `CognitionStateStore` 写入 SQLite/WAL。
- **优化结果**：目标、情绪、自我经历和未解决问题可以连续保存；当前只更新 shadow state，不越权发送消息或执行副作用。

#### 2. 单轮编排

- **v0.3**：上下文、模型调用和消息投递更多集中在 OneBot 服务中。
- **v0.4**：新增 `src/atri_qq_bot/application/turn.py`，把上下文组装、Agent 运行和交付计划固定成可追踪的事务顺序。
- **优化结果**：`ContextAssembler`、`AgentRunner`、`DeliveryPlanner` 通过 Protocol 解耦，模型、视觉和 OneBot 适配器可以替换并单独测试。

#### 3. 记忆与人格模块

- **v0.3**：记忆主要集中在 `memory.py` 和 `memory_parts/`。
- **v0.4**：拆分为 `memory/`、`persona/engine_*` 和规则模块。
- **优化结果**：加入记忆污染过滤、结构化观察、关系状态、回填和主动关怀规则，降低单体文件耦合。

#### 4. OneBot 稳定性

- **v0.3**：连接、事件、投递和主动规划集中在单体服务中。
- **v0.4**：拆到 `src/atri_qq_bot/onebot/server_parts/`，增加每会话队列、防抖合并、顺序投递、健康探测、视觉上下文 TTL 和超时降级。
- **优化结果**：慢模型或媒体任务不容易打乱同一会话的消息顺序。

#### 5. 回复质量与群聊总结

- **v0.3**：没有独立的系统化质量旁路。
- **v0.4**：新增 `src/atri_qq_bot/insights/`，使用 SQLite store、异步 evaluation worker、六维 LLM 评测、语义评分和群聊主题聚类。
- **优化结果**：评测、归档和总结在旁路运行，不阻塞主回复；WebUI 可以追踪质量趋势和总结报告。

#### 6. WebUI 架构

- **v0.3**：页面集中在 `src/atri_webui/page.py`，开发入口较重。
- **v0.4**：拆为 `page/body_*`、`page/script_*` 和 `server_parts/handler_*`。
- **优化结果**：评测、群聊总结、音乐和用户目录页面可以独立扩展和回归测试。

#### 7. QQ 空间扩展

- **v0.3**：没有项目内 QQ 空间扩展边界。
- **v0.4**：曾以 `plugins/qqzone/`、`qzone_admin.py` 和 `qzone_automation/` 通过本机 Bridge 执行。
- **优化结果**：现为 `atri_qq_bot/qzone/` 会话复用版——Cookie 经 OneBot `get_cookies` 从 NapCat 按需借取，无独立登录与常驻保活；发说说仅限主人「发条说说」口令，「更新日志」口令触发开发手记闭环；点赞与评论仅主人私聊可用。

#### 8. 音乐与资源治理

- **v0.3**：已有语音/歌声能力，但运行入口和资源竞争更集中。
- **v0.4**：新增 `music/library.py`、`music/session.py`、运行时服务和跨进程推理锁；Ollama 按真实请求启动，推理完成后按配置释放模型。
- **优化结果**：降低聊天、视觉、embedding 和语音之间的 GPU 争用。

#### 9. 发布工程

- **v0.3**：本地配置、运行数据和源码边界不够清晰。
- **v0.4**：更新 `.env.example`、`.gitignore` 和 `uv.lock`，加入 LangGraph、SQLite checkpoint 及 Insights 可选依赖。
- **优化结果**：公开仓库只保留可复现的源码、文档和示例配置。

详细的评测、群聊总结和隐私配置见 [`docs/insights.md`](docs/insights.md)。

---

## 小白启动方式

最简单：

1. 从桌宠菜单选择“启动亚托莉”，或运行 `tools\launch\qq_legacy\start-with-qq.vbs`。
2. 启动器先让 ATRI 监听 OneBot，再用独立隐藏进程恢复项目专用 NapCat/QQ。
3. Ollama 与语音服务按真实请求启动；QQ 空间不启动任何外部进程，Cookie 通过 NapCat 按需借取。
4. 如果 NapCat 掉线或 QQ 登录失效，请打开 NapCat WebUI 手动扫码登录；项目不会自动弹出二维码。
5. 用另一个 QQ 给机器人账号发送私聊消息测试。

第一次安装或需要重建 `.venv` 时运行 `tools\launch\atri\start-atri.bat`。正常启动不再执行依赖安装，也不会绕过统一启动器。

停止机器人：

- 平时不用手动停止，关闭 QQ 即可。
- 如果需要彻底停止，可以运行项目根目录里的 `停止亚托莉.bat`。

NapCat 的 OneBot 反向 WebSocket 已经自动配置为：`ws://127.0.0.1:8765/onebot`。项目只有一个运行入口；已移除会重复安装依赖并直接启动主进程的旧 `run.ps1`。

这是一个最小可运行的 NapCat / OneBot v11 反向 WebSocket 机器人，用配置的机器人 QQ 接收消息并按"亚托莉"人设回复。

## 当前完成内容

当前 `main` 对应 v0.5，以下按“核心链路、v0.5 新能力、v0.4 新能力、既有能力、工程化优化”列出已实现内容：

### 核心聊天链路

- NapCat 通过 OneBot v11 反向 WebSocket 接入；支持私聊、群聊、@触发、回复策略、消息拆分和 OneBot action 投递。
- `ConversationTurn` 固定单轮顺序：上下文组装 → Agent/工具调用 → 交付计划；工具调用写入 receipt，失败时禁止模型伪造成功结果。
- 每个会话独立排队和防抖合并，配合健康探测、重连提示、超时和文字降级，避免慢模型或媒体任务造成消息乱序。
- `AtriReplyEngine` 负责人格提示词、原作 lore、记忆上下文、语言护栏、纠错重写、群聊策略和最终回复质量门。

### v0.5 新能力

- **学术论文检索**：`papers/` 模块 + `search_papers` 工具（OpenAlex 免 key），相关度候选池内支持 cited/newest 重排，找论文/文献/研究进展时模型自主调用，严禁编造文献。
- **屏幕看护围观**：主人指令自动截屏生成画面摘要（敏感窗口打闸）；“看着我 xx”开启持续围观，帧差分触发短评、最小间隔 3 分钟，失败自动收工；所有话术由主聊天模型动态生成。
- **回复错乱修复**：任务状态账本（工具回执/确定性命令记账 + 相对时间标签注入）、自洽审查防线（规则检测“声称待办”与账本矛盾后重写）、轮次观测日志（reply-events.jsonl 补全工具回执/矛盾审查/账本快照）。
- **群记忆归属**：群主会话记忆条目记录发言群友，渲染与检索两通道按主语标注，旧数据降级为“有位群友”，不再张冠李戴。
- **QQ 空间（会话复用版）**：`atri_qq_bot/qzone/` 重构上线——Cookie 经 OneBot `get_cookies` 从 NapCat 按需借取，无独立登录与常驻保活；发说说仅限主人「发条说说」口令，「更新日志」口令触发开发手记闭环；保留每日锚点自动评论与聊天随机触发；qzone_* 自然语言工具的点赞/评论仅主人私聊可用；旧 Bridge 链路与 WebUI「QQ动态」面板全部移除，数据零迁移。

### v0.4 新能力

- **持续认知**：`cognition/` 使用 LangGraph `StateGraph` 建立“理解 → 能力提案”的显式拓扑；`CognitionKernel` 更新目标、情绪、自我经历、当前思考和未解决问题，SQLite/WAL 负责持久化。当前为 shadow mode，不直接产生副作用。
- **回复评测**：`insights/evaluation.py` 通过后台 worker 评估相关性、准确性、完整性、连贯性、忠实性和幻觉风险；支持复用当前聊天模型，也支持独立评测模型。
- **群聊总结**：归档群消息后使用 `jieba`/规则预处理、主题聚类和 LLM 总结，支持最近 24 小时、7 天或自然月的日报/周报/月报，并在 WebUI 展示报告。
- **音乐工作流**：`music/library.py` 管理完整歌曲目录和 QQ 点歌匹配，`music/session.py` 管理上传、工程阶段、版本和产物；WebUI 支持人声分离、分段、推理、混音和恢复。
- **模块化 WebUI**：原集中页面拆为 `page/body_*`、`page/script_*`，服务端拆为 `server_parts/handler_*`，新增评测、群总结、用户目录和音乐管理页面。

### v0.3 能力在 v0.4 中继续可用

- RAG 记忆：L1/L2/L3 用户记忆、SQLite FTS5 检索、可选 Ollama `bge-m3` 二次召回和原作 lore 检索。
- LLM 工具箱：时间、联网搜索、网页正文、文档/PDF、表格、图片/OCR、视频关键帧等工具，通过 Function Calling 和统一 Tool Registry 调度。
- 主动陪伴：早安、空闲关怀、群聊冷场插话、用户习惯记录、自迭代纠错和表情包触发。
- 语音与歌声：独立 HTTP 语音服务支持 ASR/TTS、GPT-SoVITS、Edge-TTS、SenseVoice；歌声管线支持人声分离、乐理分段、工程混音和质量门控。

### 工程化与性能优化

- **GPU 资源治理**：跨进程文件锁 + 优先级队列，优先级为聊天 > 用户媒体 > 视觉 > 后台；Ollama 在真实需要时启动，推理结束按配置卸载模型。
- **故障隔离**：评测、群总结、认知影子、QQ 空间和语音均为可选边界；旁路初始化失败不会阻塞基础 QQ 回复。
- **数据隔离**：长期用户记忆、认知状态、评测数据库、QQ 空间自动化数据库和媒体缓存分开存放，均由 `.env`/`data/` 路径控制。
- **可测试性**：新增 cognition、insights、qzone、music、启动链、推理锁和多模态测试；应用层通过 Protocol 降低对具体模型、OneBot 和媒体实现的耦合。
- **开源安全**：`.env.example` 使用占位符，`.gitignore` 排除密钥、聊天历史、SQLite、模型、缓存、音乐素材和 IDE 配置。

## 安装

### 方式一：一键安装（推荐）

双击项目根目录的 `安装亚托莉.bat`，向导依次完成：

1. 环境预检：Python 3.11+（或 uv）、磁盘空间、QQ 客户端检测
2. Python 环境：虚拟环境 + 依赖安装（可选清华镜像）、`.env` 生成、交互填写 `BOT_QQ`
3. 协议端：NapCat 检测与下载引导，`NAPCAT_DIR` 自动写入 `.env`
4. 启动验证：拉起 ATRI + QQ 扫码登录，自动轮询 8765 连接
5. 可选组件：桌宠快捷方式、开机自启、Ollama 与 DeepSeek key 说明

各阶段幂等，中断后重跑会自动跳过已完成步骤；安装日志见 `tools/install/install.log`。

### 方式二：手动安装

```powershell
cd "C:\Path\To\AI_ATRI"  # 替换为你的实际路径
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

如需接入大模型，编辑 `.env`：

```env
OPENAI_API_KEY=你的_API_Key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
```

不填 `OPENAI_API_KEY` 也能运行，只是会使用本地规则回复。

## 启动机器人服务

```powershell
cd "C:\Path\To\AI_ATRI"  # 替换为你的实际路径
.\.venv\Scripts\Activate.ps1
python -m atri_qq_bot
```

启动后会监听：

```text
ws://127.0.0.1:8765/onebot
```

## NapCat 配置

在 NapCat 中登录你的机器人 QQ，添加一个 OneBot v11 反向 WebSocket 连接：

- 类型：反向 WebSocket / WebSocket Client
- 地址：`ws://127.0.0.1:8765/onebot`
- OneBot 版本：v11

然后用另一个 QQ 给机器人 QQ 发私聊消息，应该能收到亚托莉风格回复。

### QQ 空间（会话复用版）

QQ 空间功能不独立登录、不常驻保活：Cookie 通过 OneBot `get_cookies(domain="user.qzone.qq.com")` 从 NapCat 按需借取，完全复用 NapCat 的唯一登录会话，流量只在实际使用时产生。在 `.env` 设置 `QZONE_ENABLED=true` 并重启亚托莉即可开启。

**主人私聊口令**（确定性拦截，仅主人可用，不需要 @）：

- 「发条说说」——发布一条说说。带正文（如「发条说说：今天试了新闹钟」）直接发布原文；不带正文时按当日主题轮换由模型生成，生成稿必须通过质量闸（反编造、反套话、排重、称呼禁令）才发布。
- 「更新日志」——执行一遍开发日志闭环：采集新提交 → 生成亚托莉口吻的开发手记 → 发布到 QQ 空间。

**自然语言工具**（`QZONE_AI_TOOLS_ENABLED=true`）：聊天中可直接说「看看好友动态」「帮我点赞/评论某某的动态」，点赞与评论仅主人在私聊会话中可用；重复点赞、重复评论由动作日志幂等拦截。

**自动评论**：每天在锚点时间（默认 23:30 ± 10 分钟抖动）随机执行一轮，拉取好友动态、跳过自己和已评论过的，由模型生成评论；`QZONE_AUTO_COMMENT_*` 可调锚点、抖动与条数。聊天中另有小概率（默认 0.0005）随机触发一条好友动态评论，带冷却防连发。

**开发日志安全闸**：只有 `QZONE_DEVLOG_VISIBILITY_TYPES`（默认 feat/fix/perf/refactor）类型的提交进入手记；标题含链接、文件路径、密钥、邮箱的提交直接跳过；每天最多 `QZONE_DEVLOG_DAILY_CAP` 篇（默认 2）。首次开启只初始化水位，不会把历史提交一次性广播；草稿 → publishing → published 状态机由 SQLite 原子更新保证幂等，发布超时按"可能已发出"处理、放弃自动重发。数据沿用 `data/qzone_automation.sqlite3`，旧版本升级无需迁移。

## Windows 双端部署（Docker 聊天 + 宿主机语音）

Windows 上推荐的结构：**聊天运行在 Docker**（NapCat + 亚托莉），**语音合成运行在宿主机**（GPT-SoVITS + `atri_voice_service`），两端通过 `host.docker.internal` 互通。

```text
QQ ⇄ NapCat(:6099) ⇄ atri-qq-bot(:8765)
                        ↓
      host.docker.internal:8790 (atri_voice_service)
                        ↓
              :9880 (GPT-SoVITS api_v2)
```

### 前置环境

- Windows + Docker Desktop（需支持 `host.docker.internal` 与目录挂载）。
- Python 3.11（完整安装，含 pip / venv）。
- NVIDIA 显卡驱动；PyTorch 需与显卡型号匹配（RTX 50 系用 cu128，不要用旧的 cu124）。
- 配好项目根 `.env`（`BOT_QQ` / `OWNER_QQ` / 模型 API）。

### 宿主机一次性准备（模型与语音运行时）

1. 下载 GPT-SoVITS 预训练模型与 ATRI 候选音色，放到 `data\models\AI_ATRI\voice\`（`base` + `candidates`）。
2. 建立 ASCII 联接以规避中文路径问题：

   ```powershell
   New-Item -ItemType Junction -Path D:\AtriModels -Target "<LOCAL_MODELS_ROOT>\AI_ATRI"
   ```

   > 脚本默认读取 `<LOCAL_MODELS_ROOT>\AI_ATRI`，可用环境变量 `LOCAL_MODELS_ROOT` 覆盖。

3. 安装 GPT-SoVITS 源码与虚拟环境到 `data\runtime\gpt-sovits\`（`tools/voice/download_gpt_sovits_source.py` + `tools/voice/install_gpt_sovits_runtime.py`）。
4. 安装语音运行时（FunASR / SenseVoice + `atri_voice_service`）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\voice\setup-voice-runtime.ps1
   ```

### 日常启动顺序

先启动宿主机语音，再启动 Docker 聊天：

```powershell
# 语音引擎
powershell -ExecutionPolicy Bypass -File tools\voice\start-gpt-sovits.ps1    # → :9880
powershell -ExecutionPolicy Bypass -File tools\voice\start-voice-service.ps1 # → :8790

# QQ 机器人（聊天）
cd deploy\docker
docker compose up -d --build
```

`.env` 关键语音项：

```env
VOICE_ASR_ENABLED=true
VOICE_TTS_ENABLED=true
VOICE_SERVICE_URL=http://host.docker.internal:8790
VOICE_PROFILE=atri-official-v2pro-curated        # 按你的候选音色档案填写
VOICE_ZH_PROFILE=
```

### NapCat 登录与接线

1. 打开 NapCat WebUI（token 见 `docker logs napcat`）。
2. 机器人 QQ 扫码登录。
3. 反向 WebSocket 指向容器内地址：`ws://atri:8765/onebot`（同一 compose 网络内使用服务名）。
4. 确认亚托莉已连接；管理面板：`http://127.0.0.1:8787`。

### 验收

- GPT-SoVITS：`http://127.0.0.1:9880` 可访问。
- 语音服务：`http://127.0.0.1:8790/health` 返回 `{"ok": true, "ready": true}`。
- WebUI「测试」页：ASR / TTS 试听通过。
- QQ 私聊发「用语音回复我」应收到语音；合成失败会自动降级为文字，不影响主聊天。

### 停止

```powershell
powershell -ExecutionPolicy Bypass -File tools\voice\stop-gpt-sovits.ps1
powershell -ExecutionPolicy Bypass -File tools\voice\stop-voice-service.ps1
cd deploy\docker; docker compose down
```

### 常见踩坑（已实测处理）

| 问题 | 处理 |
|------|------|
| Docker 读不到宿主机合成文件 | 挂载 `E:\...\data\... → /app/data/...`，NapCat 侧用 `base64://` 发送 |
| 全量 `docker build` 太慢 | 项目根加 `.dockerignore`，排除 `models` / `runtime` / `cache` |
| 改代码不想等重建 | `docker cp` 进容器后 `docker restart atri-qq-bot` |
| torchcodec / 音频加载失败 | 语音兼容层回退 `soundfile` |

日常重启只需：GPT-SoVITS → voice service → `docker compose up -d`。

## 配置项

`.env` 中常用配置：

```env
BOT_QQ=YOUR_BOT_QQ
HOST=127.0.0.1
PORT=8765
REPLY_MODE=mention
MESSAGE_SPLIT_MAX_CHARS=44
MESSAGE_SPLIT_MAX_PARTS=4
STICKER_CHANCE=0.24
IDLE_PROACTIVE_ENABLED=true
IDLE_MINUTES=180
IDLE_COOLDOWN_MINUTES=720
OWNER_QQ=
GROUP_CONTEXT_ENABLED=true
GROUP_PROACTIVE_ENABLED=true
GROUP_PROACTIVE_IDLE_MINUTES=90
GROUP_PROACTIVE_COOLDOWN_MINUTES=240
GROUP_PROACTIVE_DAILY_LIMIT=3
MORNING_GREETING_ENABLED=true
MORNING_GREETING_TIME=07:30
MORNING_GREETING_TIMEZONE=Asia/Shanghai
TOOLBOX_ENABLED=true
TOOLBOX_VISION_ENABLED=true
TOOLBOX_VISION_MODEL=qwen2.5vl:3b
TOOLBOX_VIDEO_FRAME_ANALYSIS_ENABLED=true
TOOLBOX_VIDEO_MAX_FRAMES=4
```

### v0.2 新增配置项

```env
# LLM 工具系统：模型可自主调用时间/搜索
LLM_TOOLS_ENABLED=true
LLM_TOOL_MAX_CALLS=2
WEB_SEARCH_ENABLED=true
WEB_SEARCH_TIMEOUT_SECONDS=6
WEB_SEARCH_MAX_RESULTS=5
```

### v0.5 新增配置项

```env
# 学术论文检索（OpenAlex，免 key）
PAPERS_ENABLED=true
PAPERS_TIMEOUT_SECONDS=30

# 屏幕看护围观模式
SCREEN_WATCH_PARTY_ENABLED=true
SCREEN_WATCH_PARTY_MIN_INTERVAL_SECONDS=180
SCREEN_WATCH_PARTY_SAMPLE_SECONDS=10
SCREEN_WATCH_PARTY_MAX_FAILURES=5
SCREEN_WATCH_PARTY_ANNOUNCE_COOLDOWN_SECONDS=600

# 每轮回复前系统主动联网检索一次并注入背景证据，不再依赖模型自发调用搜索工具；
# 纯问候/语气消息自动跳过。博查按次计费，高频群聊请自行评估配额。
WEB_SEARCH_PROACTIVE_ENABLED=true
```

`REPLY_MODE` 可选：

- `private`：只回复私聊。
- `mention`：回复私聊；群聊只在 @机器人或提到"亚托莉/atri"时回复。
- `all`：私聊和群聊所有消息都回复，不建议直接用于大群。

## 电脑关机也能聊

电脑关机后，本机程序不能继续运行。要做到电脑不开机也能和亚托莉聊天，需要把项目部署到一台 24 小时在线的设备上，并让 NapCat/QQ 也在那台设备上保持登录。

云端部署说明在：

```text
deploy/cloud/README.md
```

核心要求：

- 云服务器/NAS/软路由/旧电脑保持在线。
- NapCat 的 OneBot v11 反向 WebSocket 仍然连接 `ws://127.0.0.1:8765/onebot`。
- 模型接口也必须云端可用；如果仍然用本机 Ollama，电脑关机后模型会不可用。

## 每天 7:30 早安

默认已经开启。为了只发给你，建议在 `.env` 里填：

```env
OWNER_QQ=你的QQ号
```

如果不填，亚托莉会发给已经和她私聊过的人。每天只发一次，服务 7:30 附近重启也不会重复刷屏。

## 表情包

模型发送表情包只从 `.env` 的 `STICKER_DIR` 读取。当前配置的来源目录是：

```text
<Pictures>\ATRI\聊天表情
```

推荐按情绪放到这些文件夹：

```text
happy / comfort / tired / proud / confused / shy / food / goodnight / default
```

聊天记录里的图片/表情不会写回来源目录，而是自动保存到当前 BOT_QQ 的 QQ 账号记录目录：

```text
<Documents>\Tencent Files\<BOT_QQ>\ATRI\chat_history
```

可用 `STICKER_CAPTURE_DIR` 覆盖归档目录；模型发送来源仍保持为 `STICKER_DIR`。

默认联网表情缓存：

```text
<项目根目录>\data\stickers\_online_default
```

发送来源：

```text
STICKER_DIR 中的本地表情 > 网页 URL > QQ 自带表情 + emoji
```

自定义触发词编辑：

```text
<项目根目录>\data\stickers\triggers.json
```

例如 `"高性能": "proud"` 表示用户说到"高性能"时，优先从 `proud` 文件夹发一张表情包。

## 回复评测与群聊总结

WebUI 的原“开发”入口已替换为“评测”，内含回复质量看板和群聊总结；开发者状态与
NapCat 访问入口移入“测试”，实时日志保留在评测看板。该功能使用独立 SQLite 和后台
worker，不阻塞 QQ 回复。评测模型直接复用“模型”页面当前启用的聊天模型，切换模型后自动
跟随，无需在评测页面重复填写接口或 Key。安装、评分口径、定时任务与隐私说明见
[docs/insights.md](docs/insights.md)。

## 测试

```powershell
cd "C:\Path\To\AI_ATRI"  # 替换为你的实际路径
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

## 提交规范

本仓库提交信息格式为 `模块: 中文描述（关键实现点/…/单测 N 项）`。

- **模块**：改动的主要归属，如 `papers`、`screen_watch`、`persona`、`memory`、`webui`、`onebot`、`tests`、`docs`、`chore`；跨模块时取最主要的那个。
- **描述**：一句话说明意图；功能性提交在括号内列出关键实现点与新增单测数量，如 `persona: 回复错乱修复三方案（任务状态账本/自洽审查重写/轮次观测日志/确定性命令记账/单测 29 项）`。功能性新增提交的描述以「新增」开头（如 `papers: 新增学术论文检索工具（…）`），开发手记的提交分类器据此识别 `feat`，避免漏收进手记。
- **单一职责**：一笔提交只做一件事；测试随对应功能一起提交，无关重构、清理用 `chore:` 单独提交。
- **不入库内容**：运行数据、密钥、模型权重、聊天缓存与调试临时脚本（调试脚本在合入前清理，见 `chore: 清理根目录 12 个 living memory 调试遗留脚本` 的先例）。

## 开发者支持

完整的服务关系、端口、NapCat WebUI、日志、依赖与故障排查见 [DEVELOPER.md](DEVELOPER.md)。

主运行环境由 [requirements.txt](requirements.txt) 锁定：`httpx` 用于模型/API 请求，`websockets` 用于 OneBot 反向连接，`python-dotenv` 读取配置，`Pillow`/`rapidocr`/`onnxruntime` 处理图像与 OCR，`pypdf` 解析 PDF，`imageio-ffmpeg` 抽取视频帧；`pytest` 仅用于开发回归测试。语音与歌唱使用隔离运行时，不应混装进主 `.venv`。

