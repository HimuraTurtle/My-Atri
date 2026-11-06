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
| `main` | **v0.7** | 当前默认分支，跟随最新稳定代码 |
| `v0.6` | **v0.6** | 上一代发行版本：论文文档交付、可靠性巡检维护、围观旁观者重定位与跨帧连贯、DeepSeek 自研搜索 |
| `v0.5` | **v0.5** | 再上一代发行版本：学术论文检索、围观模式、回复错乱修复、群记忆归属、QQ 空间会话复用、主动联网检索与语言风格调整 |
| `v0.4` | **v0.4** | 更早的稳定版本，包含认知内核、评测、群聊总结与 QQ 空间扩展 |
| `v0.3` | **v0.3** | RAG 记忆、Agent 工具、语音/歌声与 WebUI |
| `v0.2` | **v0.2** | 包结构重构与 LLM Function Calling 工具系统 |
| `v0.1-original` | **v0.1** | 原始扁平文件版本，仅作历史存档 |

## v0.7 新增功能

v0.7 以准视觉（视觉链路的性价比与过程性）为主线。首条能力线：围观模式的多帧拼图直传——视觉模型从「一次看一帧」升级为「一次看一段时间」，同时保持单次调用成本不变。所有新增功能沿用既有降级契约：失败时坦诚说明，绝不编造结果。

### 1. 多帧拼图直传（准视觉，参考 dsh-see-now 思路自研实现）

针对「围观评论只盯着单帧、看不出过程」的信息瓶颈（截图 → 120 字描述 → 聊天模型拼过程，画面信息被文字瓶颈压缩），围观链路改为多帧拼图直传：

- **滚动帧记忆**：`WatchParty` 在敏感标题检查通过后把每帧缩到 640 宽存入滚动记忆（最多 4 帧、30 分钟窗口）；帧在视觉描述之前入记——描述失败的那一眼画面不丢，下次成功评论的拼图仍覆盖那个时刻。
- **拼图合成**（`vision.compose_sheet`）：2×2 网格把最近几帧拼成一张图，每帧左上角烧录黑底白角标（序号 + 相对时间，如 `#1 -9m`、`#4 now`）；任何一帧解码失败整图放弃，绝不发出缺帧的残缺拼图。
- **过程描述**（`vision.describe_screen_sheet`）：拼图直传视觉模型，提示词要求描述「这段时间里发生了什么」（进展/卡关/翻车/翻盘/局势起伏），输出 150 字过程描述替代单帧 120 字——一次调用看到一段过程，而非让聊天模型自己拼凑文字历史。像素预算与原单帧全宽截图同级，视觉调用成本不变。
- **三级降级**：围观刚开始只有一帧 → 退回单帧描述（旁观者提示词）；拼图合成失败 → 退回最新一帧单帧描述；合成异常不冒泡（与 `describe_screen` 同一「永不中断」契约）。
- **隐私红线不动**：敏感窗口（密码/支付/隐私类标题）在入帧之前打闸，画面字节绝不进滚动帧记忆；「在干嘛」问答仍走单帧全宽路径，语义是快照问答，不受影响。
- **实现边界**：只借鉴 dsh-see-now 的拼图直传思路（其积分判据被本项目的 3 分钟冷却替代），纯 Python/PIL 自研实现，不引入 ffmpeg/Node 依赖，规避其 PolyForm Noncommercial 许可证的代码复用限制。
- 新增配套单测 10 项（角标格式、网格几何与奇数帧补位、坏帧整图放弃、单帧路由、拼图提示词与合成图验证、合成失败回退、空帧、帧记忆裁剪与窗口、描述失败不丢帧、敏感不入帧断言）；全量回归 936 项通过。

### 2. 说话方式规范（`speech_style.md`，优先于人格语言手感）

针对「语言输出死板、像客服」的问题，引入独立的说话方式层，注入所有开口场景（主聊天/主动消息/围观评论），并在文档内声明**优先于人格核心的语言手感**——人格决定「她是谁、怎么想」，说话方式决定「她怎么开口」：

- **规范内容**：一条消息 1~20 字为主、口语碎片（省略主语、「草」「绝了」「啊？」）、句尾不加句号、不用 Markdown；语气词一条最多一个、不重复句式、冷淡敷衍是红线；高情商接话（先接情绪再回内容、先肯定再补充、不拿反问挡问题、偶尔叛逆）；一次 1~3 条消息的节奏；AI 味黑名单（「首先/其次」「希望这个回答对你有帮助」等出现即失败）；傲娇/撒娇/炸毛的分寸与反应多样性。
- **分条机制**：模型主动换行 = 想另发一条（每行一条消息），传输层 `split_reply_text` 改为跨行绝不合并（行内短句仍缝合），多条之间沿用既有的拟人发送延迟——「想补一句就发下一条」从提示词落到真实消息节奏。
- **适用边界**：正经要信息（搜索结果/资料整理/教程/文档交付）时内容完整优先，条数字数放宽，但语气底线（先接情绪、不客服腔、不 Markdown 装饰）仍生效——避免规范误伤功能性交付。
- **人格侧同步让位**：`atri_persona.md` 语言手感节删除与新规范冲突的条目（单独问号、emoji 全面禁令、粗俗词界定），明确「草」「乐」「难绷」是网络语气词不算脏话。
- **对抗式审查修复四处在既有管线上会被新风格误伤的隐患**：末行截断时整段多行回复被丢弃（`_repair_incomplete_reply` 改为多行感知——未完成只丢末行，完整前行保留）；修剪多余问号时把多行压成一行（`_trim_extra_questions` 按行修剪保行边界）；单字反应「6」「草」被重写门拦截（`_needs_rewrite` 只拦空白与纯标点）；围观插话多行输出折叠为单条（插话是一条说完的语义，评论字数同步下调为 15~50 字）。
- 新增配套单测 11 项（拆分跨行不合并/行内仍缝合/多行封顶不截断、repair 丢行不丢段、问号修剪保行边界、单字反应放行纯标点拦截、speech_style 三消费点注入与优先级锚点、围观折行）；全量回归 947 项通过。

#### 2.1 性格条目对齐文本蒸馏人格

复查说话方式规范中涉及性格的条目与 `atri_persona.md`（文本蒸馏人格）的冲突，按「性格与价值冲突时以人格核心为准」收敛（语言风格维持不变）：

- **双轨优先级声明**：文档开头改为「本节只裁定怎么开口（长短、节奏、语气词、分条）；性格、价值观和反应优先级以人格核心为准」——开口形态冲突以说话方式为准，性格冲突以人格为准。
- **删除圆滑规则**：「先肯定再补充」「别当第一个反驳的人」「不当众纠错」与人格的直率底色（天然直球指出、直率评价、先给结论）冲突，整组改为「先接话，再直说」——直率是天性，不当老好人；直说不等于刻薄，纠错就事论事、不连环追击。「先接情绪再回内容」补边界：要答案时先给结论，「先补位」是天性，不硬套共情。
- **抗议机制接入原作资产**：傲娇炸毛节补人格的分级抗议（先客气警告→「事已过三」才搬「机器人保护法」「反歧视法」，搬条文时在条文号处卡壳→「看我火箭拳制裁……」几乎不动真格；孩子气直球反击），示例措辞同步替换（「欠揍」→「想挨火箭拳」、「我要报警了」→「我要搬机器人保护法了」）。
- **修复活跃兜底文案称呼违规两处**：`_stance_fallback`「哼，我偏心主人」改为不含称呼表述、`idle_nudge_text`「看看主人有没有把自己累坏」改为「看看你」——两处均为非 owner 用户可触达的活跃路径，违反「主人」称呼红线（`engine_context._fallback_reply` 内三处同名文案位于不可达死代码，未触达不处理）。
- 新增配套单测 2 项（idle 提醒全候选无「主人」、stance 兜底无「主人」）+ speech_style 锚点断言更新（以人格核心为准、直球红线、原作资产在位）；全量回归 949 项通过。

### 3. 对称修图（引用图片 + 「对称」，纯本地确定性命令）

用户引用一张图片说「（上/下/左/右）对称」，ATRI 把指定半区镜像后覆盖对侧并回发。对称化是纯像素操作（crop + 翻转 + paste），模型在这里只会引入幻觉和延迟——触发词 + 引用图片的强前置信号让确定性拦截比 LLM 工具调用更可靠，且零 API 成本：

- **触发契约**（`image_edit/trigger.py`）：只认以「对称 / 镜像」结尾的祈使表达（容忍「一下」「处理」叠缀、称呼/@昵称前缀、尾部语气词与标点），并解析方向词——「左/左边/左半/左右」→左、「右/右边/右半」→右、「上/上面/上半/上下」→上、「下/下面/下半」→下，无方向词默认左（历史缺省）；「左右」「上下」必须先于「右」「下」匹配（长度降序），否则「左右对称」会被读成右。否定式不触发——无论否定词紧贴动词（「不要对称」）还是在方向词之前（「不要右边对称」，剥掉方向词后对剩余前缀再查一次）。「垂直/水平对称」口语歧义太大，维持放行给聊天模型——误触发比放行更有害。
- **图片定位**：被引用消息优先（复用 `_resolve_quoted_message` 链路），其次当前消息自带图片；闪照与商店表情不参与修图。**没有可用图片时放行给聊天模型**——「这构图好对称」这类闲聊不会被误拦，图片前置条件本身就是主护栏。
- **素材候选链**（`image_edit/fetch.py`，与工具箱 `_resolve_ref_material` 同构）：段内 url → NapCat 本地缓存路径 → `get_image` 动作回退；流式下载超 30MB 中止。
- **镜像几何**（`image_edit/mirror.py`）：四方向共用同一骨架——「取源半区 → 沿垂直于对称轴的方向翻转 → 贴到目标半区」，`mid = ceil(w/2)`（或 `ceil(h/2)`）——奇数宽/高时中列/中行恰好落在对称轴上保持不变（数学上正确的镜像对称）；RGBA 透明通道与 P 调色板模式原样保留，源图不被修改；不认识的方向抛 ValueError（方向只能来自触发层解析，不接受自由文本）；带 EXIF 方向的手机竖拍照先 `exif_transpose` 摆正显示方向再镜像，避免对称轴被旋转。
- **动图对称**（`image_edit/gif.py`，v0.7 补充）：GIF 帧间是增量编码（每帧只存变化区域），「每一帧都对称」的正确路径是把每帧完整合成出来再镜像——对增量帧直接替换左半会把上一帧的残影镜像进结果（Pillow `seek` 已按 disposal 语义自动合成增量帧）；输出端统一写全帧（`disposal=2` 帧帧替换），从根上杜绝输出 GIF 再依赖增量合成。资源契约对抗病态 GIF：单帧边长超 1024px 先降采样（镜像不需要超清保真）、帧数超 300 按步长采样且被跳帧时长累到保留帧上（总播放时长守恒）、输出超 25MB 时用 640px 重编码一次、仍超预算则坦诚失败不发出半残结果；透明通道经 255 号调色板索引往返保留。
- **执行与降级契约**：主线程只做头部探测（懒加载，不解码像素）分流动图/静态，全部像素编解码在默认线程池执行、不阻塞事件循环（静态 30 秒 / 动图 90 秒超时）；静态图 EXIF 摆正后长边 4096px 封顶。下载/解码/处理失败时坦诚短句告知（「这张图我拿不下来，稍后再试试」「这张图我打不开呀，格式好像不对」「这张图/动图我处理不动呀，稍后再试试」），绝不编造结果——Pillow 对损坏文件抛什么异常没有稳定契约（实测截断 GIF 的 `n_frames` 扫描抛的是 `IndexError` 而非 `EOFError`），探测层对一切异常降级、绝不穿透到事件层；处理超时后线程仍可能在写文件，动图清理延迟 180 秒盖过 90 秒处理窗口。
- **拦截点**：与漫画/头像命令同级（回复门控之后、聊天模型之前）；群聊遵循既有回复门控（@ 或智能模式），私聊直达。
- 新增配套单测 104 项（镜像几何：四方向偶/奇边长中轴不变/透明通道/调色板/源图不变/未知方向拒绝；触发词：方向解析矩阵 37 组命中（含左右全称、半字族、@前缀、动作叠缀）+ 16 组拒绝（否定在动词前与方向词前、垂直/水平歧义、闲聊）；候选链：url 优先/本地路径/get_image 回退/全链失败返回 None；动图对称：逐帧镜像/方向参数逐帧生效/增量帧先合成/透明往返/零时长归一/循环数保留/350 帧采样时长守恒/大图降采样/截断 GIF 只发已解码帧；命令接线：静态 PNG（含「下对称」方向端到端）与动图 GIF 双路径回发/EXIF 摆正/当前消息图片兜底/无图放行/下载与解码失败坦诚/探测异常不穿透（含真实截断 GIF 集成用例）/静动图超时与写入失败坦诚且清理延迟正确/静态大图 4096 封顶/非触发文本不触碰引用读取）。

## v0.6 新增功能

v0.6 的四条能力线：论文检索结果的文档化交付（一键整理成 Word 发送）、全功能巡检与可靠性维护（交付滞留/内存增长/无限表三个隐含问题修复）、围观模式从“画面播报”重定位为“旁观者感受”（含跨帧观察连贯性），以及搜索引擎从博查整体切换为 DeepSeek 自研搜索。所有新增功能沿用既有降级契约：失败时坦诚说明，绝不编造结果。

### 1. 论文整理文档（`compile_paper_digest`）

- **触发方式**：用户在看过本会话的论文检索结果后说「整理成文档」「总结成文件」「发我一份」（私聊与群聊均可），聊天模型调用 `compile_paper_digest` 工具；默认整理最近一次 `search_papers` 的全部结果，用户点名「第 1 篇和第 3 篇」时用 indices 按检索顺序选取。
- **文档结构**：检索信息（关键词/排序/时间）→ 模型综述（可选，由模型基于检索结果撰写研究脉络与方法对比，传参嵌入）→ 速览列表 → 逐篇信息卡（标题/作者/年份/期刊/被引/原文链接/摘要）；元数据全部来自检索结果原样转写，零编造。
- **零依赖 docx 生成**：`papers/docx_writer.py` 用标准库（zipfile + OOXML）手写极简 Word 写入器——标题/小节/正文/列表/超链接全部直接格式化，不依赖 styles.xml 与 numbering.xml，Word、WPS 与手机 QQ 文件预览均可打开；docx 生成异常时自动降级为同结构 Markdown 文件。
- **会话级检索缓存**：每会话保留最近一次成功检索（30 分钟 TTL、64 会话上限），整理时直接取用，不重复请求 OpenAlex。
- **投递链路**：文档落盘 `data/runtime/paper_digests/`（保留最新 40 份）后排入交付队列，OneBot 层在文字回复发出后经 `upload_group_file` / `upload_private_file` 发送，与漫画/绘画交付同一模式；投递失败发坦诚提示文案，禁止谎称已发送。
- 新增配套单测 20 项（docx 包结构与 XML 转义、降级 md、文件名清洗、目录容量清理、缓存 TTL 与容量上限、indices 校验与排序、执行器接线、投递 mixin 三态）；全量回归 914 项通过。

### 2. 全功能巡检与可靠性维护（2026-11-01）

对运行日志、回复观测数据与全部能力模块做了一轮系统巡检（日志无 ERROR、观测无异常），修复三个隐含问题：

- **回复失败时交付物滞留队列**：回复文字生成失败（或 OneBot 未确认发送）的两条路径原先直接返回，本轮工具排队的交付物（漫画章节/绘画/论文整理文档）会滞留内存，等到该会话下一条消息才脱离语境地补发——论文文档还可能已被 40 份轮转清理删除，届时凭空发出"发送失败"提示。现在四路投递统一收敛为 `_deliver_pending_artifacts`，三条路径（正常回复/文字失败/发送未确认）都会投递：这些文件是用户本轮明确索要的真实成果，不应随文字失败一起丢失。
- **主动检索备忘录无限增长**：`_proactive_search_memo` 每会话缓存一份证据文本（可达数千字）但无容量上限、过期条目永不回收，长运行下缓慢累积内存。现在设 128 会话上限：超限时先剔除过期条目（TTL 600 秒），仍超限则按写入时间丢最旧。
- **两张只增不删的表**：`qzone_automation_logs`（动作幂等日志）与 `screen_summaries`（屏幕摘要）此前无任何删除策略。现在分别保留 180 天 / 30 天，采用写入时节流清理（每 24 小时至多执行一次 DELETE），不额外引入定时器；清理失败仅告警，不影响写入。

新增配套单测 6 项（备忘录容量上限、两表保留策略与节流、回复失败仍投递的端到端用例）；全量回归 920 项通过。

### 3. 围观模式定位调整：从“画面播报”到“旁观者感受”（2026-11-01）

针对用户反馈（围观评论像在描述屏幕、不像真人在旁边看），重写两处提示词：

- **视觉描述层**（`vision.py`）：从“识别前台应用与窗口标题”改为“旁观者视角看状态”——玩什么/做什么、局势或进度如何（打到哪、顺不顺、卡在哪、有什么值得反应的点），并明确忽略帧率、CPU/内存占用等性能监控数据、硬件悬浮窗与系统状态栏；“在干嘛”问答沿用该描述（被问才具体说明，与图片回复行为一致）。
- **围观评论层**（`party.py`）：从“可以好奇、吐槽、鼓励，或点评画面里的细节”改为“坐在旁边的朋友”——优先发表**自己的感受**（惊叹/着急/心疼/鼓励/吐槽，如「好难啊」「好菜啊」「这波可以」），而不是复述画面里有什么；像真人旁观者那样说，不用句句工整；不提帧率、CPU 占用这类性能数据；感受必须有画面依据，不许编造。
- **观察连贯性**：`WatchParty` 新增滚动画面记忆（最近 5 眼、30 分钟窗口，相同描述只刷新时间），评论时把“从早到近”的连续画面注入提示词，并要求评论对**一段连续变化**（进展、卡关、翻车、翻盘）反应——比如看到“开局满血打 Boss → Boss 血量见底 → 团灭结算”，评论的是“又没打过，好难啊”这样的过程感受，而不是单帧画面的复述。
- 反幻觉与称呼红线（不编造画面、禁用“主人”、失败静默跳过）全部保留；新增 3 项测试（旁观者定位断言、历史块注入、tick 历史链路），全量回归 923 项通过。

### 4. 搜索引擎替换：博查 → DeepSeek 自研搜索（2026-11-03，v0.6 收官改动）

针对博查搜索效率与准确度不佳的反馈，联网搜索主通道整体切换为 DeepSeek 官方 Search API（自研搜索引擎），博查链路全部移除：

- **接口与计费**：`POST api.deepseek.com/v1/tools/search`，5 元/1000 次成功请求（无结果不计费），Search/Fetch 独立限速 100 QPS；`need_content=false` 模式使用面向模型阅读设计的 `summary` 字段（600~800 字符）。
- **质量信号注入**：每条结果带 `rerank_score`（0~3 相关度）与 `authority`（0~4 权威度），随 `source_type` 一并注入证据，帮助模型识别优质来源；发布时间从 UTC RFC3339 统一转成北京时间。
- **搜索语法编译**：`_deepseek_query` 把搜索意图编译成引擎原生的 `site:`（多域名 OR）、`lang:zh`（news 模式）与 `after:` 时效过滤——原博查 freshness 分级（今天→当天、最近/最新/近况→7 天、今年/去年→365 天、news 默认→30 天）无缝映射为 `after:` 日期，普通网页查询不加过滤。
- **key 自动复用**：解析顺序 `DEEPSEEK_SEARCH_API_KEY` > 视觉通道的 `TOOLBOX_VISION_API_KEY`（当 `TOOLBOX_VISION_BASE_URL` 指向 `api.deepseek.com` 时）——视觉模型已用 DeepSeek 官方 key，零新增账号成本；配置项 `bocha_api_key` 移除，`search_provider` 值域改为 `auto`/`deepseek`/`rss`。
- **同步替换的桥接**：anima 兼容层的 `tavily_search` 替身改调 DeepSeek 端点（搜索开关同样跟随新 key）；主动检索、memes 垂直站搜索的时效/域名行为不变。
- **降级契约不变**：DeepSeek 失败（欠费/超时/网络）自动退回免费 RSS 链（Bing/Google News），搜索能力永不中断；真实冒烟延迟 0.88 秒（timeout 6 秒充足）。
- 测试改写与新增 4 项（after 日期分级、搜索语法编译、UTC 日期转换、结果解析与质量信号），全量回归 926 项通过。

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
# 纯问候/语气消息自动跳过。DeepSeek Search 按次计费（5 元/1000 次成功请求），
# 高频群聊请自行评估配额。
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

