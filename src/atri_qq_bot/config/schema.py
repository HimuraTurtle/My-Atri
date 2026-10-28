"""ATRI 的不可变运行配置契约。

``BotConfig`` 只保存经过 loader 校验后的值：OneBot 地址、回复策略、
模型与多模态开关、语音、WebUI。业务模块只读取它，配置修改由 WebUI
写入 .env 后重新加载，避免运行中出现半更新状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .env import _project_root


VALID_REPLY_MODES = {"private", "mention", "smart", "all"}


@dataclass(frozen=True)
class BotConfig:
    # OneBot 反向 WebSocket：NapCat 连接到 host:port/onebot。
    bot_qq: int
    host: str
    port: int
    reply_mode: str  # private / mention / smart / all，见 VALID_REPLY_MODES。
    openai_api_key: str | None
    openai_base_url: str
    openai_model: str
    temperature: float
    max_tokens: int
    frequency_penalty: float = 0.25
    # 单次模型请求和 GPU 排队分别设上限，避免一个失败轮次拖住整个会话。
    chat_request_timeout_seconds: float = 25.0
    chat_resource_wait_seconds: float = 35.0
    chat_quality_retries: int = 1
    message_split_max_chars: int = 44
    message_split_max_parts: int = 4
    message_send_delay_min: float = 0.55
    message_send_delay_max: float = 1.35
    sticker_dir: Path = field(default_factory=lambda: _project_root() / "data" / "stickers")
    sticker_trigger_file: Path = field(
        default_factory=lambda: _project_root() / "data" / "stickers" / "triggers.json"
    )
    # Incoming chat stickers belong to the configured QQ account record tree,
    # not to the local library used by the model when it sends stickers.
    sticker_capture_dir: Path = field(
        default_factory=lambda: Path.home()
        / "Documents"
        / "Tencent Files"
        / "0"
        / "ATRI"
        / "chat_history"
    )
    sticker_chance: float = 0.24
    sticker_cooldown_seconds: int = 120
    sticker_capture_enabled: bool = True
    sticker_capture_max_bytes: int = 3_000_000
    memory_path: Path = field(default_factory=lambda: _project_root() / "data" / "memory" / "users.json")
    # JSON 是可编辑事实源；SQLite 仅作可删除、可重建的检索索引。
    memory_retrieval_enabled: bool = True
    memory_retrieval_path: Path = field(
        default_factory=lambda: _project_root() / "data" / "memory" / "semantic_memory.sqlite3"
    )
    memory_retrieval_max_chars: int = 1_800
    memory_retrieval_lore_limit: int = 3
    memory_retrieval_memory_limit: int = 5
    # 默认关闭向量调用：bge-m3 会按需占用额外内存，FTS5 主路径始终可用。
    memory_vector_enabled: bool = False
    memory_embedding_model: str = "bge-m3:latest"
    memory_embedding_base_url: str = "http://127.0.0.1:11434"
    memory_embedding_timeout_seconds: float = 2.0
    # 记忆提取在独立后台线程执行；即使本地模型超时也不影响回复链路。
    memory_extraction_enabled: bool = True
    memory_extraction_llm_enabled: bool = True
    memory_extraction_model: str = "qwen3:4b-instruct"
    memory_extraction_base_url: str = "http://127.0.0.1:11434"
    memory_extraction_api_key: str | None = None
    memory_extraction_timeout_seconds: float = 10.0
    memory_extraction_cooldown_seconds: float = 45.0
    # 启动后低优先级回放已有 history；只写入有证据的显式事实/重复偏好。
    memory_backfill_enabled: bool = True
    memory_backfill_max_conversations: int = 250
    # LivingMemory 情节记忆：对话攒批 -> LLM 总结 -> 混合检索 -> TTL 衰减。
    living_memory_enabled: bool = True
    living_memory_trigger_messages: int = 16
    living_memory_max_batch_messages: int = 40
    living_memory_top_k: int = 4
    living_memory_min_importance: float = 0.25
    living_memory_recent_count: int = 1
    living_memory_recent_max_age_hours: float = 72.0
    living_memory_embedding_enabled: bool = True
    living_memory_llm_timeout_seconds: float = 60.0
    living_memory_maintenance_interval_hours: float = 6.0
    living_memory_forget_delay_days: float = 7.0
    living_memory_purge_delay_days: float = 30.0
    # 群聊长时间静默（断线/整夜无话）后，第一条新消息把缺口前话题回填进 L2。
    gap_backfill_enabled: bool = True
    gap_backfill_threshold_seconds: int = 7200
    gap_backfill_lookback_seconds: int = 21600
    proactive_v2_enabled: bool = False
    idle_proactive_enabled: bool = True
    idle_minutes: int = 180
    idle_cooldown_minutes: int = 720
    idle_check_seconds: int = 60
    group_context_enabled: bool = True
    group_proactive_enabled: bool = True
    group_proactive_idle_minutes: int = 90
    group_proactive_cooldown_minutes: int = 240
    group_proactive_daily_limit: int = 3
    group_proactive_max_silence_days: int = 3
    group_proactive_check_seconds: int = 90
    owner_qqs: tuple[int, ...] = ()
    morning_greeting_enabled: bool = True
    morning_greeting_time: str = "07:30"
    morning_greeting_timezone: str = "Asia/Shanghai"
    morning_greeting_catchup_minutes: int = 90
    # 晚安问候：每晚固定时间向机器人加入的所有群聊发送晚安图与晚安语句。
    goodnight_enabled: bool = True
    goodnight_time: str = "23:30"
    goodnight_timezone: str = "Asia/Shanghai"
    goodnight_catchup_minutes: int = 30
    goodnight_image: Path = field(
        default_factory=lambda: _project_root() / "data" / "assets" / "goodnight.jpg"
    )
    goodnight_state_path: Path = field(
        default_factory=lambda: _project_root() / "data" / "runtime" / "goodnight-state.json"
    )
    toolbox_enabled: bool = True
    toolbox_timeout_seconds: float = 8.0
    toolbox_max_bytes: int = 2_000_000
    toolbox_max_document_bytes: int = 20_000_000
    toolbox_max_media_bytes: int = 80_000_000
    toolbox_vision_enabled: bool = False
    toolbox_vision_model: str = ""
    toolbox_vision_fallback_model: str = ""
    toolbox_vision_base_url: str = ""
    # 可选：视觉兜底模型的独立接口地址。未填写时，本地模型名自动使用本机 Ollama。
    toolbox_vision_fallback_base_url: str = ""
    toolbox_vision_api_key: str | None = None
    toolbox_vision_max_bytes: int = 8_000_000
    toolbox_vision_retry_count: int = 1
    toolbox_vision_resource_wait_seconds: float = 10.0
    toolbox_vision_timeout_seconds: float = 55.0
    toolbox_vision_unload_other_ollama_models: bool = False
    toolbox_ocr_enabled: bool = False
    # SauceNAO 以图搜图 API key（免费注册）；空=只用 trace.moe 匿名搜图。
    saucenao_api_key: str = ""
    # AnimeTrace 二次元角色库人脸匹配（免密钥公开服务，移植自 astrbot_plugin_shitu）；
    # 命中直接参与身份裁决，与 trace.moe（集数出处）互补。
    animetrace_enabled: bool = True
    toolbox_video_frame_analysis_enabled: bool = True
    toolbox_video_max_frames: int = 4
    # 单条视频消息的总处理预算，默认 60 秒，超时自动降级到 OCR/标题。
    toolbox_video_deadline_seconds: float = 60.0
    llm_tools_enabled: bool = True
    llm_tool_max_calls: int = 2
    llm_agent_protocol_enabled: bool = False
    llm_agent_max_steps: int = 6
    llm_agent_max_tool_calls: int = 3
    human_reply_pipeline_enabled: bool = True
    # 新认知内核先以只读影子模式运行：维护持续目标、自我和情绪，
    # 但不发送消息、不调用副作用能力，也不写入 L1/L2/L3。
    cognition_shadow_enabled: bool = True
    cognition_state_path: Path = field(
        default_factory=lambda: _project_root() / "data" / "cognition" / "state.sqlite3"
    )
    cognition_shadow_timeout_seconds: float = 2.0
    cognition_shadow_max_concurrency: int = 2
    web_search_enabled: bool = True
    web_search_timeout_seconds: float = 6.0
    web_search_max_results: int = 5
    # 每轮回复前系统侧主动联网检索一次并注入证据，不依赖模型自发调用工具。
    # 纯问候/语气消息自动跳过；注意博查按次计费，高频群聊请评估配额。
    web_search_proactive_enabled: bool = True
    web_page_max_chars: int = 8_000
    # 搜索提供方：auto=有博查 key 用博查否则 RSS；rss=只用免费 RSS。
    search_provider: str = "auto"
    bocha_api_key: str | None = None
    web_grounding_review_enabled: bool = True
    online_music_enabled: bool = True
    # Suwayomi 漫画助手：GraphQL 客户端、订阅推送与 LLM 工具。
    manga_enabled: bool = True
    manga_server_url: str = "http://127.0.0.1:4567"
    manga_auth_mode: str = "none"
    manga_username: str = ""
    manga_password: str = ""
    manga_max_pages: int = 20
    manga_download_format: str = "pdf"
    manga_download_concurrency: int = 6
    manga_download_retries: int = 3
    manga_check_interval_minutes: int = 60
    manga_chapter_cache_hours: int = 6
    manga_default_source_id: int = 0
    manga_ai_tools_enabled: bool = True
    manga_allow_ai_send: bool = True
    manga_ai_max_sources: int = 5
    manga_ai_results_per_source: int = 5
    manga_ai_tool_timeout_seconds: float = 60.0
    manga_auto_push_mode: str = "image"
    manga_temp_dir: str = ""
    manga_store_path: Path = field(
        default_factory=lambda: _project_root() / "data" / "manga" / "store.json"
    )
    # Anima 绘画助手（移植自 astrbot_plugin_anima_master）：自然语言 →
    # Danbooru tag 提示词管线 → 本机 ComfyUI 文生图。ComfyUI 未部署前
    # 保持关闭；运行细节配置在 data/anima/config.json 独立管理。
    anima_enabled: bool = False
    # 提示词优化 LLM：留空时沿用主聊天模型接口。
    anima_prompt_model: str = ""
    anima_prompt_base_url: str = ""
    anima_prompt_api_key: str | None = None
    anima_prompt_timeout_seconds: float = 90.0
    # anima_draw 工具整体执行预算（含提示词优化 + ComfyUI 排队出图）。
    anima_tool_timeout_seconds: float = 420.0
    # MiniMax 绘图（image-01 / image-01-live 云端文生图）：自然语言 →
    # draw_image 工具 → MiniMax API 出图，无本地 GPU 依赖。
    minimax_draw_enabled: bool = False
    minimax_api_key: str | None = None
    # 默认国内站，海外填 https://api.minimaxi.com。
    minimax_base_url: str = "https://api.minimax.cn"
    minimax_timeout_seconds: float = 120.0
    # 画图前用 LLM 精炼提示词：扩写画面细节，涉及具体人物时联网检索外貌。
    minimax_prompt_refine_enabled: bool = True
    # 事实核查（移植自 AstrBot「是真的吗」插件）：fact_check 工具，
    # LLM 规划 → Bocha 搜索取证 → LLM 三态裁决（真/假/无法核实）。
    isittrue_enabled: bool = False
    isittrue_timeout_seconds: float = 120.0
    # 学术文献检索：search_papers 工具，OpenAlex 免 key 全学科文献库，
    # 用户找论文/文献/研究进展时由聊天模型自主调用。
    papers_enabled: bool = True
    papers_timeout_seconds: float = 30.0
    # QQ 空间（移植自 astrbot_plugin_qzone，会话复用版）：不再独立登录，
    # Cookie 通过 OneBot get_cookies 从 NapCat 按需借取，无独立保活。
    qzone_enabled: bool = False
    # qzone_* 自然语言工具（看动态/点赞/评论，点赞评论仅主人私聊可用）。
    qzone_ai_tools_enabled: bool = True
    qzone_timeout_seconds: float = 20.0
    qzone_session_ttl_seconds: float = 600.0
    # 说说默认可见性：1=公开 2=仅自己（发布与主人口令共用）。
    qzone_visibility: int = 1
    qzone_timezone: str = "Asia/Shanghai"
    # 自动评论：每天一个锚点时间 ± 抖动分钟内随机执行一次。
    qzone_auto_comment_enabled: bool = True
    qzone_auto_comment_time: str = "23:30"
    qzone_auto_comment_jitter_minutes: int = 10
    # 每轮自动评论的好友动态条数上限。
    qzone_auto_comment_max: int = 2
    # 聊天随机触发评论：每条参与会话的消息按此概率在后台评论一条好友动态。
    qzone_random_comment_probability: float = 0.0005
    # 两次随机触发之间的最小间隔（分钟），防连发。
    qzone_random_comment_cooldown_minutes: int = 30
    # 开发日志闭环：「更新日志」主人口令触发；提交白名单 + 每日上限。
    qzone_devlog_enabled: bool = True
    qzone_devlog_auto_publish: bool = True
    qzone_devlog_daily_cap: int = 2
    qzone_devlog_visibility_types: tuple[str, ...] = ("feat", "fix", "perf", "refactor")
    # 热梗自动学习：定时抓取热搜并用聊天模型精选，聊天时注入最新梗。
    meme_learning_enabled: bool = True
    meme_learning_interval_hours: int = 6
    meme_context_max_items: int = 12
    # B站视频解析与 AI 总结（移植自 AstrBot biliVideo 插件）。
    bilibili_enabled: bool = True
    # 纯链接/分享卡片消息自动触发总结（关闭时仅响应「B站总结」命令）。
    bilibili_auto_summary: bool = False
    # 总结风格：concise / detailed / professional。
    bilibili_note_style: str = "professional"
    # 可选登录凭据（SESSDATA 等浏览器 Cookie，多个用分号分隔），提高字幕可见性。
    bilibili_cookie: str = ""
    # 无字幕视频下载音频走必剪 ASR 转写。
    bilibili_asr_enabled: bool = True
    # 命令冷却秒数（按用户），自动总结另有按会话冷却（180 秒）。
    bilibili_cooldown_seconds: int = 20
    bilibili_max_note_length: int = 3500
    bilibili_timeout_seconds: float = 300.0
    bilibili_audio_dir: str = ""
    # bilibili_read LLM 工具（BiliRead 理念移植）：把字幕/转写素材交回
    # 对话模型，由人设融合口吻聊视频；关闭后仅保留「B站总结」命令。
    bilibili_ai_tool_enabled: bool = True
    bilibili_ai_tool_timeout_seconds: float = 150.0
    bilibili_ai_tool_max_chars: int = 4000
    # 「是啊，吃什么」群聊接梗（移植自 astrbot_plugin_what_to_eat）。
    # 被动触发：群消息含关键词即响应，不要求 @ 机器人；私聊交给聊天模型。
    what_to_eat_enabled: bool = True
    what_to_eat_trigger_keywords: tuple[str, ...] = ("吃什么", "吃啥")
    # 命中后推荐食物的概率（推荐先冒螃蟹兴奋再转实际推荐），其余概率复读。
    what_to_eat_recommend_probability: float = 0.5
    # 食物候选：内置列表（默认大学生常吃 50 种 + 人设螃蟹）+ 自定义列表，
    # 合并去重后随机。
    what_to_eat_builtin_foods: tuple[str, ...] = (
        "黄焖鸡米饭", "麻辣烫", "兰州拉面", "沙县小吃", "重庆小面",
        "螺蛳粉", "米线", "炒饭", "炒面", "盖浇饭",
        "水饺", "馄饨", "煎饼果子", "肉夹馍", "烤冷面",
        "关东煮", "冒菜", "香锅", "炸鸡", "汉堡",
        "披萨", "寿司", "便当", "凉皮", "凉面",
        "热干面", "酸辣粉", "炸酱面", "牛肉面", "叉烧饭",
        "烧腊饭", "煲仔饭", "石锅拌饭", "部队锅", "烤肉饭",
        "猪脚饭", "卤肉饭", "烤鸭饭", "口水鸡", "酸菜鱼",
        "水煮鱼", "毛血旺", "干锅", "烤鱼", "烧烤",
        "奶茶+面包", "便利店", "泡面", "食堂自选", "轻食沙拉",
        "螃蟹",
    )
    what_to_eat_custom_foods: tuple[str, ...] = ()
    # 频率限制：时间窗口内最大响应次数，超出后强制推荐，打断多 Bot 复读接龙。
    what_to_eat_rate_limit_max: int = 3
    what_to_eat_rate_limit_window_seconds: int = 60
    # 复读后冷却期内强制推荐，防 Bot 之间互相复读循环。
    what_to_eat_echo_cooldown_seconds: int = 15
    # 屏幕看护（自建轻量实时监控）：1fps 帧差分采样，画面语义变化时
    # 调用视觉模型生成摘要；指令层（看看我屏幕等）独立于后台监控开关。
    screen_watch_enabled: bool = True
    # 后台实时监控总开关：关闭时仅保留主人指令能力。
    screen_watch_monitor_enabled: bool = False
    # 差分采样间隔（秒），决定「实时」感知粒度。
    screen_watch_interval_seconds: float = 1.0
    # 帧差分灰度均值阈值：越小越灵敏（弹窗闪烁易误触）。
    screen_watch_diff_threshold: float = 8.0
    # 两次视觉调用间的最小间隔（秒）：视频/游戏持续变化时防止烧 API。
    screen_watch_cooldown_seconds: float = 120.0
    # 持续变化退避的上限（秒）：高频触发时冷却指数增长到此封顶。
    screen_watch_backoff_max_seconds: float = 300.0
    # 围观模式：主人说「看着我xx」后，ATRI 持续看屏并向该会话发围观短评；
    # 也允许 ATRI 检测到主人的活动宣告后主动围观。结束由主人话语触发。
    screen_watch_party_enabled: bool = True
    # 围观评论的最小间隔（秒）：画面显著变化才评，且至少隔这么久。
    screen_watch_party_min_interval_seconds: float = 180.0
    # 围观循环的采样间隔（秒）：比入库监控粗，confirm 需要连续两帧变化。
    screen_watch_party_sample_seconds: float = 10.0
    # 连续截图/视觉失败达到该次数自动收工（坦诚告知，不空转）。
    screen_watch_party_max_failures: int = 5
    # 主动围观分类的冷却（秒）：控制活动宣告识别的 LLM 调用频率。
    screen_watch_party_announce_cooldown_seconds: float = 600.0
    # Who Is Who 群聊共享上下文（移植自 astrbot_plugin_who_is_who）：
    # 本地规则记录群成员身份/称呼/话题/承诺，回复前注入共享上下文，
    # 不增加额外 LLM 调用。
    wiw_enabled: bool = True
    # 运行模式：record_only（仅入库）/ identity_only（+身份）/
    # context（+相关消息）/ full（+状态账本与承诺裁决）。
    wiw_mode: str = "full"
    wiw_db_path: Path = field(
        default_factory=lambda: _project_root() / "data" / "who_is_who" / "who_is_who.db"
    )
    # 自动学习「以后叫我XX」「@某人 绰号叫XX」类称呼请求。
    wiw_learn_nicknames: bool = True
    # 检测「以后都简短回复」类长期请求，由 AI 回复裁决是否形成承诺。
    wiw_detect_commitments: bool = True
    # 话题归属：N 分钟内的词汇重叠消息归入同一话题。
    wiw_topic_active_minutes: int = 30
    wiw_max_messages_per_group: int = 500
    wiw_retention_days: int = 30
    # 群开关：enabled 为空=全部群；blocked 恒优先排除。
    wiw_enabled_groups: tuple[str, ...] = ()
    wiw_blocked_groups: tuple[str, ...] = ()
    # 插件管理员默认继承 owner_qqs；restricted 用户的规则请求一律拒绝。
    wiw_admin_ids: tuple[str, ...] = ()
    wiw_restricted_ids: tuple[str, ...] = ()
    # 显式白名单 JSON：[{"user_id":"123","group_id":"456","priority":60,
    # "permissions":["create_persistent_rule"]}]。
    wiw_explicit_users_json: str = ""
    wiw_default_priority: int = 10
    wiw_authority_enabled: bool = True
    # 本人偏好称呼优先于管理员以外的所有提议。
    wiw_self_preference_first: bool = True
    wiw_allow_whitelist_override: bool = False
    wiw_newer_rule_wins: bool = True
    # 共享上下文规模上限：相关消息/成员/状态条数与注入字符数。
    wiw_max_related_messages: int = 8
    wiw_max_related_people: int = 4
    wiw_max_related_states: int = 6
    wiw_max_injection_chars: int = 1600
    # 表情包小偷（移植自 astrbot_plugin_stealer）：被动偷取群里出现的
    # 表情包，VLM 分类后入发送库；分类端点沿用 toolbox_vision_* 配置。
    meme_steal_enabled: bool = True
    # probability 按概率偷；cooldown 两次偷取至少间隔 N 秒（默认，防刷）。
    meme_steal_mode: str = "cooldown"
    meme_steal_chance: float = 0.5
    meme_steal_cooldown_seconds: int = 20
    meme_steal_max_bytes: int = 3_000_000
    # 发送库内偷取文件（steal_ 前缀）数量上限，超出按最旧淘汰；自带
    # 表情包不带 steal_ 前缀，永不参与淘汰。
    meme_steal_library_max: int = 300
    meme_steal_queue_size: int = 8
    # 发送时偷取表情包的占比：0.3 ≈ 自带:偷取 7:3（先掷来源骰子再在
    # 对应来源内语义选图）；0 表示不区分来源，沿用全库混选。
    meme_steal_send_ratio: float = 0.3
    # 相册直发图片（无表情包标记的普通 image 段）是否也作为候选交给
    # VLM 判定：判定为照片/截图/二维码会丢弃并缓存哈希不再重判。相册
    # 直发是群里分享梗图的主要形态之一，开启后漏偷大幅减少，代价是
    # 新照片会各消耗一次 VLM 调用（受偷取冷却限流）。
    meme_steal_plain_images: bool = True
    # 启动期依赖服务引导：拉起 Suwayomi-Server 与 NapCat，见 runtime/service_bootstrap.py。
    bootstrap_services_enabled: bool = True
    bootstrap_suwayomi_enabled: bool = True
    bootstrap_suwayomi_script: Path = field(
        default_factory=lambda: _project_root().parent / "Suwayomi" / "start-suwayomi.ps1"
    )
    bootstrap_suwayomi_timeout_seconds: float = 120.0
    bootstrap_napcat_enabled: bool = True
    # WebUI 仅允许绑定回环地址，避免把本机管理能力暴露到局域网。
    webui_enabled: bool = True
    webui_host: str = "127.0.0.1"
    webui_port: int = 8787

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)
