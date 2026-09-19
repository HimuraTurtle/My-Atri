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
