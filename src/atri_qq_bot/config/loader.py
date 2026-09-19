"""从 .env 读取并约束运行配置。

所有数值范围在此处收敛，例如工具调用次数、视频抽帧数和语音长度；下游
模块因此不应自行解析环境变量。修改 .env 后经 WebUI 的 reload_config
或重启服务生效。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from .env import (
    _env,
    _env_bool,
    _env_float,
    _env_int,
    _env_int_tuple,
    _env_path,
    _optional_env,
    _project_root,
)
from .schema import VALID_REPLY_MODES, BotConfig


def load_config(env_file: str | Path | None = None) -> BotConfig:
    """加载指定 .env 并返回已校验配置；非法回复模式会立即报错。"""
    root = _project_root()
    if env_file:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(root / ".env", override=True)

    reply_mode = _env("REPLY_MODE", "mention").lower()
    if reply_mode not in VALID_REPLY_MODES:
        modes = ", ".join(sorted(VALID_REPLY_MODES))
        raise ValueError(f"REPLY_MODE must be one of: {modes}")

    openai_base_url = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    openai_model = _supported_model_alias(
        openai_base_url,
        _env("OPENAI_MODEL", "gpt-4.1-mini"),
    )
    openai_api_key = _optional_env("OPENAI_API_KEY")
    bot_qq = _env_int("BOT_QQ", 0)
    qq_records_root = _env_path(
        "QQ_RECORDS_ROOT",
        Path.home() / "Documents" / "Tencent Files",
        root,
    )
    return BotConfig(
        bot_qq=bot_qq,
        host=_env("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8765),
        reply_mode=reply_mode,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=openai_model,
        temperature=_env_float("TEMPERATURE", 0.8),
        frequency_penalty=_env_float("FREQUENCY_PENALTY", 0.25),
        max_tokens=_env_int("MAX_TOKENS", 350),
        chat_request_timeout_seconds=max(
            5.0, min(60.0, _env_float("CHAT_REQUEST_TIMEOUT_SECONDS", 25.0))
        ),
        chat_resource_wait_seconds=max(
            5.0, min(60.0, _env_float("CHAT_RESOURCE_WAIT_SECONDS", 35.0))
        ),
        chat_quality_retries=max(
            0, min(2, _env_int("CHAT_QUALITY_RETRIES", 1))
        ),
        message_split_max_chars=_env_int("MESSAGE_SPLIT_MAX_CHARS", 44),
        message_split_max_parts=_env_int("MESSAGE_SPLIT_MAX_PARTS", 4),
        message_send_delay_min=_env_float("MESSAGE_SEND_DELAY_MIN", 0.55),
        message_send_delay_max=_env_float("MESSAGE_SEND_DELAY_MAX", 1.35),
        sticker_dir=_env_path("STICKER_DIR", root / "data" / "stickers", root),
        sticker_trigger_file=_env_path(
            "STICKER_TRIGGER_FILE", root / "data" / "stickers" / "triggers.json", root
        ),
        sticker_capture_dir=_env_path(
            "STICKER_CAPTURE_DIR",
            qq_records_root / str(bot_qq) / "ATRI" / "chat_history",
            root,
        ),
        sticker_chance=_env_float("STICKER_CHANCE", 0.24),
        sticker_cooldown_seconds=_env_int("STICKER_COOLDOWN_SECONDS", 120),
        sticker_capture_enabled=_env_bool("STICKER_CAPTURE_ENABLED", True),
        sticker_capture_max_bytes=_env_int("STICKER_CAPTURE_MAX_BYTES", 3_000_000),
        memory_path=_env_path("MEMORY_PATH", root / "data" / "memory" / "users.json", root),
        memory_retrieval_enabled=_env_bool("MEMORY_RETRIEVAL_ENABLED", True),
        memory_retrieval_path=_env_path(
            "MEMORY_RETRIEVAL_PATH",
            root / "data" / "memory" / "semantic_memory.sqlite3",
            root,
        ),
        memory_retrieval_max_chars=max(
            800, min(3_000, _env_int("MEMORY_RETRIEVAL_MAX_CHARS", 1_800))
        ),
        memory_retrieval_lore_limit=max(
            1, min(5, _env_int("MEMORY_RETRIEVAL_LORE_LIMIT", 3))
        ),
        memory_retrieval_memory_limit=max(
            2, min(8, _env_int("MEMORY_RETRIEVAL_MEMORY_LIMIT", 5))
        ),
        memory_vector_enabled=_env_bool("MEMORY_VECTOR_ENABLED", False),
        memory_embedding_model=_env("MEMORY_EMBEDDING_MODEL", "bge-m3:latest"),
        memory_embedding_base_url=_env(
            "MEMORY_EMBEDDING_BASE_URL", "http://127.0.0.1:11434"
        ).rstrip("/"),
        memory_embedding_timeout_seconds=max(
            0.5, min(20.0, _env_float("MEMORY_EMBEDDING_TIMEOUT_SECONDS", 2.0))
        ),
        memory_extraction_enabled=_env_bool("MEMORY_EXTRACTION_ENABLED", True),
        memory_extraction_llm_enabled=_env_bool("MEMORY_EXTRACTION_LLM_ENABLED", True),
        memory_extraction_model=_env("MEMORY_EXTRACTION_MODEL", "qwen3:4b-instruct"),
        memory_extraction_base_url=_env(
            "MEMORY_EXTRACTION_BASE_URL", openai_base_url
        ).rstrip("/"),
        memory_extraction_api_key=_optional_env("MEMORY_EXTRACTION_API_KEY") or openai_api_key,
        memory_extraction_timeout_seconds=max(
            3.0, min(30.0, _env_float("MEMORY_EXTRACTION_TIMEOUT_SECONDS", 10.0))
        ),
        memory_extraction_cooldown_seconds=max(
            5.0, min(300.0, _env_float("MEMORY_EXTRACTION_COOLDOWN_SECONDS", 45.0))
        ),
        memory_backfill_enabled=_env_bool("MEMORY_BACKFILL_ENABLED", True),
        memory_backfill_max_conversations=max(
            10, min(500, _env_int("MEMORY_BACKFILL_MAX_CONVERSATIONS", 250))
        ),
        living_memory_enabled=_env_bool("LIVING_MEMORY_ENABLED", True),
        living_memory_trigger_messages=max(
            6, _env_int("LIVING_MEMORY_TRIGGER_MESSAGES", 16)
        ),
        living_memory_max_batch_messages=max(
            10, min(80, _env_int("LIVING_MEMORY_MAX_BATCH_MESSAGES", 40))
        ),
        living_memory_top_k=max(1, min(8, _env_int("LIVING_MEMORY_TOP_K", 4))),
        living_memory_min_importance=max(
            0.0, min(1.0, _env_float("LIVING_MEMORY_MIN_IMPORTANCE", 0.25))
        ),
        living_memory_recent_count=max(0, min(4, _env_int("LIVING_MEMORY_RECENT_COUNT", 1))),
        living_memory_recent_max_age_hours=max(
            1.0, _env_float("LIVING_MEMORY_RECENT_MAX_AGE_HOURS", 72.0)
        ),
        living_memory_embedding_enabled=_env_bool("LIVING_MEMORY_EMBEDDING_ENABLED", True),
        living_memory_llm_timeout_seconds=max(
            10.0, min(180.0, _env_float("LIVING_MEMORY_LLM_TIMEOUT_SECONDS", 60.0))
        ),
        living_memory_maintenance_interval_hours=max(
            0.5, _env_float("LIVING_MEMORY_MAINTENANCE_INTERVAL_HOURS", 6.0)
        ),
        living_memory_forget_delay_days=max(
            1.0, _env_float("LIVING_MEMORY_FORGET_DELAY_DAYS", 7.0)
        ),
        living_memory_purge_delay_days=max(
            7.0, _env_float("LIVING_MEMORY_PURGE_DELAY_DAYS", 30.0)
        ),
        gap_backfill_enabled=_env_bool("GAP_BACKFILL_ENABLED", True),
        gap_backfill_threshold_seconds=max(
            600, _env_int("GAP_BACKFILL_THRESHOLD_SECONDS", 7200)
        ),
        gap_backfill_lookback_seconds=max(
            1800, _env_int("GAP_BACKFILL_LOOKBACK_SECONDS", 21600)
        ),
        manga_enabled=_env_bool("MANGA_ENABLED", True),
        manga_server_url=_env("MANGA_SERVER_URL", "http://127.0.0.1:4567").rstrip("/"),
        manga_auth_mode=_env("MANGA_AUTH_MODE", "none"),
        manga_username=_env("MANGA_USERNAME", ""),
        manga_password=_env("MANGA_PASSWORD", ""),
        manga_max_pages=max(1, _env_int("MANGA_MAX_PAGES", 20)),
        manga_download_format=_env("MANGA_DOWNLOAD_FORMAT", "pdf"),
        manga_download_concurrency=max(
            1, min(16, _env_int("MANGA_DOWNLOAD_CONCURRENCY", 6))
        ),
        manga_download_retries=max(0, _env_int("MANGA_DOWNLOAD_RETRIES", 3)),
        manga_check_interval_minutes=max(
            5, _env_int("MANGA_CHECK_INTERVAL_MINUTES", 60)
        ),
        manga_chapter_cache_hours=max(
            -1, _env_int("MANGA_CHAPTER_CACHE_HOURS", 6)
        ),
        manga_default_source_id=_env_int("MANGA_DEFAULT_SOURCE_ID", 0),
        manga_ai_tools_enabled=_env_bool("MANGA_AI_TOOLS_ENABLED", True),
        manga_allow_ai_send=_env_bool("MANGA_ALLOW_AI_SEND", True),
        manga_ai_max_sources=max(
            1, min(20, _env_int("MANGA_AI_MAX_SOURCES", 5))
        ),
        manga_ai_results_per_source=max(
            1, min(20, _env_int("MANGA_AI_RESULTS_PER_SOURCE", 5))
        ),
        manga_ai_tool_timeout_seconds=max(
            5.0, min(300.0, _env_float("MANGA_AI_TOOL_TIMEOUT_SECONDS", 60.0))
        ),
        manga_auto_push_mode=_env("MANGA_AUTO_PUSH_MODE", "image"),
        manga_temp_dir=_env("MANGA_TEMP_DIR", ""),
        bootstrap_services_enabled=_env_bool("BOOTSTRAP_SERVICES_ENABLED", True),
        bootstrap_suwayomi_enabled=_env_bool("BOOTSTRAP_SUWAYOMI_ENABLED", True),
        bootstrap_suwayomi_script=Path(
            _env(
                "BOOTSTRAP_SUWAYOMI_SCRIPT",
                str(_project_root().parent / "Suwayomi" / "start-suwayomi.ps1"),
            )
        ),
        bootstrap_suwayomi_timeout_seconds=max(
            10.0, _env_float("BOOTSTRAP_SUWAYOMI_TIMEOUT_SECONDS", 120.0)
        ),
        bootstrap_napcat_enabled=_env_bool("BOOTSTRAP_NAPCAT_ENABLED", True),
        proactive_v2_enabled=_env_bool("PROACTIVE_V2_ENABLED", False),
        idle_proactive_enabled=_env_bool("IDLE_PROACTIVE_ENABLED", True),
        idle_minutes=_env_int("IDLE_MINUTES", 180),
        idle_cooldown_minutes=_env_int("IDLE_COOLDOWN_MINUTES", 720),
        idle_check_seconds=_env_int("IDLE_CHECK_SECONDS", 60),
        group_context_enabled=_env_bool("GROUP_CONTEXT_ENABLED", True),
        group_proactive_enabled=_env_bool("GROUP_PROACTIVE_ENABLED", True),
        group_proactive_idle_minutes=_env_int("GROUP_PROACTIVE_IDLE_MINUTES", 90),
        group_proactive_cooldown_minutes=_env_int("GROUP_PROACTIVE_COOLDOWN_MINUTES", 240),
        group_proactive_daily_limit=min(3, _env_int("GROUP_PROACTIVE_DAILY_LIMIT", 3)),
        group_proactive_max_silence_days=max(0, _env_int("GROUP_PROACTIVE_MAX_SILENCE_DAYS", 3)),
        group_proactive_check_seconds=_env_int("GROUP_PROACTIVE_CHECK_SECONDS", 90),
        owner_qqs=_env_int_tuple("OWNER_QQ", ()),
        morning_greeting_enabled=_env_bool("MORNING_GREETING_ENABLED", True),
        morning_greeting_time=_env("MORNING_GREETING_TIME", "07:30"),
        morning_greeting_timezone=_env("MORNING_GREETING_TIMEZONE", "Asia/Shanghai"),
        morning_greeting_catchup_minutes=_env_int("MORNING_GREETING_CATCHUP_MINUTES", 90),
        toolbox_enabled=_env_bool("TOOLBOX_ENABLED", True),
        toolbox_timeout_seconds=_env_float("TOOLBOX_TIMEOUT_SECONDS", 8.0),
        toolbox_max_bytes=_env_int("TOOLBOX_MAX_BYTES", 2_000_000),
        toolbox_max_document_bytes=_env_int("TOOLBOX_MAX_DOCUMENT_BYTES", 20_000_000),
        toolbox_max_media_bytes=_env_int("TOOLBOX_MAX_MEDIA_BYTES", 80_000_000),
        toolbox_vision_enabled=_env_bool("TOOLBOX_VISION_ENABLED", False),
        toolbox_vision_model=_env("TOOLBOX_VISION_MODEL", ""),
        toolbox_vision_fallback_model=_env(
            "TOOLBOX_VISION_FALLBACK_MODEL",
            "",
        ),
        toolbox_vision_base_url=_env("TOOLBOX_VISION_BASE_URL", "").rstrip("/"),
        toolbox_vision_fallback_base_url=_env(
            "TOOLBOX_VISION_FALLBACK_BASE_URL", ""
        ).rstrip("/"),
        toolbox_vision_api_key=_optional_env("TOOLBOX_VISION_API_KEY"),
        toolbox_vision_max_bytes=_env_int("TOOLBOX_VISION_MAX_BYTES", 8_000_000),
        toolbox_vision_retry_count=max(
            0,
            min(3, _env_int("TOOLBOX_VISION_RETRY_COUNT", 1)),
        ),
        toolbox_vision_resource_wait_seconds=max(
            5.0,
            min(
                300.0,
                _env_float("TOOLBOX_VISION_RESOURCE_WAIT_SECONDS", 10.0),
            ),
        ),
        toolbox_vision_timeout_seconds=max(
            20.0,
            min(60.0, _env_float("TOOLBOX_VISION_TIMEOUT_SECONDS", 55.0)),
        ),
        toolbox_vision_unload_other_ollama_models=_env_bool(
            "TOOLBOX_VISION_UNLOAD_OTHER_OLLAMA_MODELS",
            True,
        ),
        toolbox_ocr_enabled=_env_bool("TOOLBOX_OCR_ENABLED", False),
        saucenao_api_key=_env("SAUCENAO_API_KEY", "").strip(),
        toolbox_video_frame_analysis_enabled=_env_bool("TOOLBOX_VIDEO_FRAME_ANALYSIS_ENABLED", True),
        toolbox_video_max_frames=max(1, min(8, _env_int("TOOLBOX_VIDEO_MAX_FRAMES", 4))),
        toolbox_video_deadline_seconds=max(
            20.0,
            min(60.0, _env_float("TOOLBOX_VIDEO_DEADLINE_SECONDS", 60.0)),
        ),
        llm_tools_enabled=_env_bool("LLM_TOOLS_ENABLED", True),
        llm_tool_max_calls=max(1, min(4, _env_int("LLM_TOOL_MAX_CALLS", 2))),
        llm_agent_protocol_enabled=_env_bool("LLM_AGENT_PROTOCOL_ENABLED", False),
        llm_agent_max_steps=max(2, min(10, _env_int("LLM_AGENT_MAX_STEPS", 6))),
        llm_agent_max_tool_calls=max(
            1,
            min(6, _env_int("LLM_AGENT_MAX_TOOL_CALLS", 3)),
        ),
        human_reply_pipeline_enabled=_env_bool("HUMAN_REPLY_PIPELINE_ENABLED", True),
        cognition_shadow_enabled=_env_bool("COGNITION_SHADOW_ENABLED", True),
        cognition_state_path=_env_path(
            "COGNITION_STATE_PATH",
            root / "data" / "cognition" / "state.sqlite3",
            root,
        ),
        cognition_shadow_timeout_seconds=max(
            0.2,
            min(10.0, _env_float("COGNITION_SHADOW_TIMEOUT_SECONDS", 2.0)),
        ),
        cognition_shadow_max_concurrency=max(
            1,
            min(4, _env_int("COGNITION_SHADOW_MAX_CONCURRENCY", 2)),
        ),
        web_search_enabled=_env_bool("WEB_SEARCH_ENABLED", True),
        web_search_timeout_seconds=_env_float("WEB_SEARCH_TIMEOUT_SECONDS", 6.0),
        web_search_max_results=max(1, min(8, _env_int("WEB_SEARCH_MAX_RESULTS", 5))),
        web_page_max_chars=max(1000, min(20_000, _env_int("WEB_PAGE_MAX_CHARS", 8_000))),
        search_provider=_env("SEARCH_PROVIDER", "auto").strip().casefold() or "auto",
        bocha_api_key=_optional_env("BOCHA_API_KEY"),
        web_grounding_review_enabled=_env_bool("WEB_GROUNDING_REVIEW_ENABLED", True),
        online_music_enabled=_env_bool("ONLINE_MUSIC_ENABLED", True),
        meme_learning_enabled=_env_bool("MEME_LEARNING_ENABLED", True),
        meme_learning_interval_hours=max(
            1, _env_int("MEME_LEARNING_INTERVAL_HOURS", 6)
        ),
        meme_context_max_items=max(0, _env_int("MEME_CONTEXT_MAX_ITEMS", 12)),
        bilibili_enabled=_env_bool("BILIBILI_ENABLED", True),
        bilibili_auto_summary=_env_bool("BILIBILI_AUTO_SUMMARY", False),
        bilibili_note_style=(
            style
            if (style := _env("BILIBILI_NOTE_STYLE", "professional").strip().lower())
            in ("concise", "detailed", "professional")
            else "professional"
        ),
        bilibili_cookie=_env("BILIBILI_COOKIE", ""),
        bilibili_asr_enabled=_env_bool("BILIBILI_ASR_ENABLED", True),
        bilibili_cooldown_seconds=max(
            0, _env_int("BILIBILI_COOLDOWN_SECONDS", 20)
        ),
        bilibili_max_note_length=max(
            500, _env_int("BILIBILI_MAX_NOTE_LENGTH", 3500)
        ),
        bilibili_timeout_seconds=max(
            30.0, _env_float("BILIBILI_TIMEOUT_SECONDS", 300.0)
        ),
        bilibili_audio_dir=_env("BILIBILI_AUDIO_DIR", ""),
        webui_enabled=_env_bool("WEBUI_ENABLED", True),
        webui_host=_env("WEBUI_HOST", "127.0.0.1"),
        webui_port=_env_int("WEBUI_PORT", 8787),
    )


def _supported_model_alias(base_url: str, model: str) -> str:
    if "api.deepseek.com" not in str(base_url).casefold():
        return model
    return {
        # V4.1 Flash 起 DeepSeek 把通用 Flash ID 更名为 deepseek-flash，
        # v4-flash/deepseek-chat 均为兼容别名，服务端统一按新名计费分组。
        "deepseek-chat": "deepseek-flash",
        "deepseek-v4-flash": "deepseek-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }.get(str(model).strip().casefold(), model)
