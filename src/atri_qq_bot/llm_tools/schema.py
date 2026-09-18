from __future__ import annotations

from typing import Any

from ..prompting import load_prompt
from ..voice.policy import load_voice_behavior
from .agent_protocol import AGENT_ACTION_PROMPT, final_answer_schema


# 工具选择原则由外部 Markdown 维护，避免模型能力规则和代码实现重复。
_TOOL_USAGE_INSTRUCTION_PROMPT = load_prompt("tool_use")
AUTOMATIC_TOOL_INSTRUCTION_PROMPT = load_prompt("tool_use")
TOOL_INSTRUCTION_PROMPT = load_prompt("tool_use")

_LEGACY_AUTOMATIC_TOOL_INSTRUCTION_PROMPT = (
    "你可以在确有需要时自主调用工具；普通闲聊、情绪回应和已有上下文足够的问题直接回答，"
    "不要为了调用而调用工具。\n"
    + _TOOL_USAGE_INSTRUCTION_PROMPT
)
# 保留旧拼装变量仅供兼容导入；实际对外使用上面的外部文档内容。


def available_tool_schemas(config: Any) -> list[dict[str, Any]]:
    if not bool(getattr(config, "llm_tools_enabled", True)):
        return []

    tools = [_current_time_schema()]
    if bool(getattr(config, "living_memory_enabled", True)):
        tools.append(_long_term_memory_schema())
    if bool(getattr(config, "manga_enabled", True)) and bool(
        getattr(config, "manga_ai_tools_enabled", True)
    ):
        tools.extend(_manga_tool_schemas(config))
    if bool(getattr(config, "web_search_enabled", True)):
        tools.append(_weather_schema())
        tools.append(_web_search_schema(config))
        tools.append(_web_page_schema())
    if bool(getattr(config, "voice_tts_enabled", False)):
        tools.append(_voice_schema(config))
        if load_voice_behavior().get("calls_enabled", False):
            tools.append(_voice_call_schema())
    if bool(getattr(config, "llm_agent_protocol_enabled", False)):
        tools.append(final_answer_schema(getattr(config, "max_tokens", 1200) * 4))
    return tools


def _long_term_memory_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "recall_long_term_memory",
            "description": (
                "召回自己关于过去对话的长期记忆（以前聊过的话题、一起经历过的事）。"
                "用户提起以前聊过的内容、问你还记不记得某件事，或当前话题明显延续很久之前的话题时使用；"
                "当前上下文已经足够时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "检索关键词，优先用话题里的具体名词（人名、作品、事件、地点），多个词用空格分隔。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回记忆条数，默认 5，最多 8。",
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": ["keywords"],
                "additionalProperties": False,
            },
        },
    }


def _current_time_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前本地时间、日期和星期。用户问现在、今天、明天、几点、星期几，或建议依赖当前时间时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "目标时区，默认 Asia/Shanghai。当前仅稳定支持 Asia/Shanghai、UTC 和 UTC±小时。",
                    }
                },
            },
        },
    }


def _manga_tool_schemas(config: Any) -> list[dict[str, Any]]:
    del config
    return [
        {
            "type": "function",
            "function": {
                "name": "suwayomi_search_manga",
                "description": (
                    "在漫画源中搜索漫画并返回候选（含 manga_id、标题、状态、来源）。"
                    "用户提到某部漫画、想看/想找某部漫画、询问有没有某漫画时使用；"
                    "拿到 manga_id 后可继续用 suwayomi_get_chapters 查章节。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "漫画名称关键词，尽量使用作品原名。",
                        },
                        "source_hint": {
                            "type": "string",
                            "description": "可选的漫画源名称或语言提示（如 拷贝、zh）；不确定时留空。",
                        },
                        "search_all_sources": {
                            "type": "boolean",
                            "description": "是否搜索全部源；默认只搜常用源，信息不足时再开启。",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suwayomi_get_chapters",
                "description": (
                    "查询一部漫画的章节列表或定位某一话，返回 chapter_id。"
                    "用户想知道更新到哪一话、想读某话、或需要确认章节号时使用；"
                    "发送章节前必须先用它确认章节存在。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "manga_id": {
                            "type": "integer",
                            "description": "漫画 ID，来自 suwayomi_search_manga 或订阅列表。",
                        },
                        "selector": {
                            "type": "string",
                            "description": (
                                "list=看最新章节列表，latest=最新一话（默认），"
                                "也可填章节号或 ID:数字 精确定位。"
                                "章节号同号有正篇和番外时通常会自动选正篇并在 note 里说明。"
                            ),
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "是否强制刷新章节缓存；默认 false。",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100,
                            "description": "返回章节列表条数，默认 20。",
                        },
                    },
                    "required": ["manga_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suwayomi_get_subscriptions",
                "description": (
                    "查看当前会话订阅的漫画列表（含 manga_id、标题、推送开关）。"
                    "用户问“我订阅了什么”、“有什么漫画在看”时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suwayomi_subscribe",
                "description": (
                    "为当前会话订阅一部漫画，有新章节时会收到更新通知。"
                    "用户明确想追某部漫画时使用；manga_id 需先通过搜索获得。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "manga_id": {
                            "type": "integer",
                            "description": "漫画 ID。",
                        },
                        "push_enabled": {
                            "type": "boolean",
                            "description": "是否同时开启自动推送章节内容，默认跟随会话设置。",
                        },
                    },
                    "required": ["manga_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suwayomi_unsubscribe",
                "description": "取消当前会话对一部漫画的订阅。用户明确不想再追时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "manga_id": {
                            "type": "integer",
                            "description": "漫画 ID。",
                        },
                    },
                    "required": ["manga_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suwayomi_send_chapter",
                "description": (
                    "把某一话漫画直接发送给当前会话（图片或打包文件）。"
                    "仅在用户明确想“看/读/发/来一话”时调用；"
                    "调用前必须先用 suwayomi_get_chapters 确认章节，chapter_id 必须来自其结果。"
                    "当你上一轮向用户询问“要哪个章节/哪个版本”、用户回复确认（如“是”“对”“第一个”）后，"
                    "必须在当前这轮直接调用本工具发送已确认的章节，不要只回复文字说“我去拿一下”而不调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "manga_id": {
                            "type": "integer",
                            "description": (
                                "漫画 ID（来自 suwayomi_search_manga 或 suwayomi_get_chapters 的 manga 字段），"
                                "注意不是章节 ID，两者不要混淆。"
                            ),
                        },
                        "chapter_id": {
                            "type": "integer",
                            "description": (
                                "章节 ID（来自 suwayomi_get_chapters 结果中的 chapter_id 字段），"
                                "与 manga_id 是不同的编号，不要混用。"
                            ),
                        },
                        "fmt": {
                            "type": "string",
                            "enum": ["image", "pdf", "zip", "cbz"],
                            "description": "发送形式：image=逐页图片（默认），其余为打包文件。",
                        },
                    },
                    "required": ["manga_id", "chapter_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _web_search_schema(config: Any) -> dict[str, Any]:
    max_results = int(getattr(config, "web_search_max_results", 5) or 5)
    return {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "搜索一般网页和最新新闻。用于实时信息、用户明确要求搜索，或核验视觉分析中不确定的人物、作品、梗图出处。"
                "网络新梗、流行语、缩写和圈内黑话，只要拿不准含义或流行状态就必须先搜索，不要凭印象回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。要具体，不要只传“最新消息”。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"返回结果数量，默认不超过 {max_results} 条。",
                        "minimum": 1,
                        "maximum": max(1, max_results),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "web", "news", "research"],
                        "description": "一般资料用 web，近期消息用 news，前沿或未解问题用 research。",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": "可选的强相关网站域名，例如 github.com、arxiv.org 或项目官网；不要填写完整 URL。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _web_page_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "open_web_page",
            "description": (
                "读取 search_web 返回的公开网页正文。用于核验具体事实、研究现状和原始来源；"
                "只打开搜索结果中确实出现的 URL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "search_web 结果中的完整 http/https URL。",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }


def _weather_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市或地区的当前天气与未来三天预报。天气、温度、降雨、体感温度问题优先使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "明确的城市或地区名称，例如“湖北十堰”或“东京”。不要猜测用户所在地。",
                    }
                },
                "required": ["location"],
            },
        },
    }


def _voice_schema(config: Any) -> dict[str, Any]:
    max_chars = max(20, int(getattr(config, "voice_max_chars", 160) or 160))
    return {
        "type": "function",
        "function": {
            "name": "speak_as_atri",
            "description": "让亚托莉用角色语音说一小段话。仅在用户想听语音或语音明显比文字更合适时调用；mode=singing 仅限用户明确要求唱歌，系统只会按歌名发送完整音乐目录中的整首成品，绝不临时合成或朗读歌词。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            f"要说出的完整口语文本，不含网址或舞台说明，最多 {max_chars} 字。"
                            "必须直接书写目标语言：中文用汉字，日语用假名/汉字，英文用英文；"
                            "禁止罗马音、拼音、语言标签和翻译说明。"
                        ),
                        "maxLength": max_chars,
                    },
                    "emotion": {
                        "type": "string",
                        "description": (
                            "这句话实际要表达的语气。neutral=平静，gentle=温柔安慰，"
                            "happy=开心/得意/玩笑，shy=害羞/撒娇，sad=低落，"
                            "serious=认真提醒，sleepy=困倦/晚安，surprised=惊讶。"
                        ),
                        "enum": [
                            "neutral",
                            "gentle",
                            "happy",
                            "shy",
                            "sad",
                            "serious",
                            "sleepy",
                            "surprised",
                        ],
                    },
                    "intensity": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "情绪强度，普通自然口语建议 0.45-0.65，"
                            "明显情绪建议 0.65-0.8，避免无故填满。"
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["auto", "zh", "ja", "en"],
                    },
                    "reason": {
                        "type": "string",
                        "enum": [
                            "explicit_request",
                            "voice_reply",
                            "autonomous",
                            "proactive",
                        ],
                        "description": "本次发语音的真实触发原因。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["speech", "singing"],
                        "description": "普通语音使用 speech；仅在用户明确要求唱歌或哼唱时使用 singing。唱歌只匹配完整音乐目录中的整首成品。",
                    },
                    "song_title": {
                        "type": "string",
                        "description": "mode=singing 时填写用户点名的已学会歌曲名；没有点名时留空，不要编造歌名。",
                        "maxLength": 120,
                    },
                    "segment_index": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 99,
                        "description": "兼容字段。mode=singing 现在始终发送整首歌曲，用户提到片段或高潮时也请留空。",
                    },
                    "segment_hint": {
                        "type": "string",
                        "description": "用户说“开头/高潮/后半段”等非编号段落时填写原意，供本地选择器兜底。",
                        "maxLength": 80,
                    },
                },
                "required": ["text", "reason"],
            },
        },
    }


def _voice_call_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "offer_voice_call",
            "description": "向当前私聊用户发起浏览器实时语音通话邀请。仅在用户明确要求或亲密且适合连续交谈时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "这次想聊的简短主题，可留空。",
                        "maxLength": 120,
                    }
                },
            },
        },
    }
