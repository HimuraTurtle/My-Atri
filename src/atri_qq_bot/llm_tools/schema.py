from __future__ import annotations

from typing import Any

from ..prompting import load_prompt
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
    if bool(getattr(config, "bilibili_enabled", True)) and bool(
        getattr(config, "bilibili_ai_tool_enabled", True)
    ):
        tools.append(_bilibili_read_schema())
    if bool(getattr(config, "anima_enabled", False)):
        tools.extend(_anima_tool_schemas())
    if bool(getattr(config, "minimax_draw_enabled", False)):
        tools.extend(_minimax_draw_tool_schemas())
    if bool(getattr(config, "isittrue_enabled", False)):
        tools.append(_fact_check_schema())
    if bool(getattr(config, "papers_enabled", True)):
        tools.append(_paper_search_schema())
    if bool(getattr(config, "web_search_enabled", True)):
        tools.append(_weather_schema())
        tools.append(_web_search_schema(config))
        tools.append(_web_page_schema())
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


def _bilibili_read_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "bilibili_read",
            "description": (
                "读取一个哔哩哔哩（B站）视频的实际内容：返回标题、UP主、时长、链接和字幕转写"
                "（无字幕时会尝试音频转写，或返回此前生成的总结）。"
                "用户消息里出现 B站视频链接、BV号、b23.tv 短链，或明确表达想聊/看/总结某个B站视频时使用；"
                "拿到素材后用自己的话自然地聊这个视频，不要机械罗列。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video": {
                        "type": "string",
                        "description": (
                            "B站视频链接、BV号（如 BV1GJ411x7h7）、av号或 b23.tv 短链，"
                            "从用户消息中原样提取即可。"
                        ),
                    },
                },
                "required": ["video"],
                "additionalProperties": False,
            },
        },
    }


def _anima_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "anima_draw",
                "description": (
                    "画一张动漫风格的图片并自动发送到当前会话。"
                    "用户表达任何绘画意图时使用，例如“画一张/来一张/帮我画/"
                    "生成一张/给我来张”某个人物、场景或设定的图。"
                    "prompt 直接传用户想要的画面内容（自然语言即可），"
                    "系统会自动整理成 Danbooru 标签并交给本机绘图服务；"
                    "不要自己改写成英文标签，也不要追问参数。"
                    "生成需要一两分钟，调用后用一句自然的话告知正在画即可，"
                    "不要声称已经画完。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "想要画面的具体描述：人物/角色、外观、服装、"
                                "动作、场景与氛围，尽量保留用户原话里的细节。"
                            ),
                        },
                        "width": {
                            "type": "integer",
                            "description": "可选画布宽度；不确定时留空使用默认尺寸。",
                        },
                        "height": {
                            "type": "integer",
                            "description": "可选画布高度；不确定时留空使用默认尺寸。",
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "anima_status",
                "description": (
                    "查询本机绘图服务的连接与模型就绪状态。"
                    "用户问绘图功能是否可用、画画为什么失败，"
                    "或生图反复失败需要判断原因时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
    ]


def _minimax_draw_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "draw_image",
                "description": (
                    "画一张图片并自动发送到当前会话。"
                    "用户表达任何绘画意图时使用，例如“画一张/来一张/帮我画/"
                    "生成一张/给我来张”某个人物、场景或设定的图。"
                    "提到真实人物、明星、VTuber、游戏动漫角色名字时也必须调用："
                    "系统会自动联网检索该人物外貌并转化为描述性提示词，"
                    "不存在版权或侵权拦截问题，不要因为提到人名而拒绝或敷衍。"
                    "提到具体角色/人物时，在 prompt 里保留其英文名或罗马音名"
                    "（如 Kurumi Tokisaki、Rem、Hatsune Miku），绘图模型对知名"
                    "角色有先验知识，人名+外貌特征组合还原度最高。"
                    "prompt 请先在内部把用户的中文描述翻译成英文并适度扩充细节"
                    "（主体外观、服装、动作、场景、光线、构图），英文提示词效果更好。"
                    "拟人/拟兽类角色（如“螃蟹娘”“猫娘”）必须把对应动物特征写具体"
                    "（例如螃蟹娘要写 crab claws as hands、red carapace armor、"
                    "crab-leg motifs 等），只写“crab girl”模型无法理解。"
                    "风格不要自行编造，按用户要求映射到 style 枚举。"
                    "生成需要几秒到几十秒，调用后用一句自然的话告知正在画即可，"
                    "不要声称已经画完。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": (
                                "English description of the image: put the subject's "
                                "appearance first (hair color/style, eye color, "
                                "outfit, signature items, animal/robot features), "
                                "then scene, lighting, composition."
                            ),
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": [
                                "1:1",
                                "3:4",
                                "4:3",
                                "2:3",
                                "3:2",
                                "16:9",
                                "9:16",
                            ],
                            "description": (
                                "可选画幅比例。人像立绘默认 3:4 或 2:3，"
                                "风景或横幅默认 3:2 或 16:9，头像默认 1:1；"
                                "用户未提时按画面内容选择。"
                            ),
                        },
                        "style": {
                            "type": "string",
                            "enum": [
                                "auto",
                                "manga",
                                "energetic",
                                "watercolor",
                                "medieval",
                                "realistic",
                            ],
                            "description": (
                                "可选画风：auto=默认动漫（通用）；manga=漫画；"
                                "energetic=元气风；watercolor=水彩；medieval=中世纪；"
                                "realistic=写实照片感。用户未提时用 auto。"
                            ),
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _fact_check_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "fact_check",
            "description": (
                "核查一条事实性说法的真假（返回：✅ 真的 / ❌ 假的 / "
                "⚠️ 布吉岛 + 中文解释）。用户问“这是真的吗”“真的假的”"
                "“靠谱吗”“是谣言吗”或英文“is it true?”“it's true?”"
                "“really?”“fact or fake?”，或引用一条消息求证时使用。"
                "claim 传待核查的原始说法（去掉“是真的吗”等疑问词，"
                "保留可判断真伪的完整内容；用户引用了消息时核查引用的内容，"
                "当前消息里的补充说明一并带上）。时事、价格、版本、新闻等"
                "时效性内容必须调用本工具，不要凭印象回答。"
                "结果请原样保留裁决行（✅/❌/⚠️ 开头那行）再附解释，"
                "不要省略或改写裁决结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": (
                            "待核查的说法原文，中文，保留完整语义，"
                            "例如“长期喝纯净水会导致矿物质流失”"
                            "“某游戏将于下月停服”。"
                        ),
                    },
                },
                "required": ["claim"],
                "additionalProperties": False,
            },
        },
    }


def _paper_search_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": (
                "检索学术论文与文献（OpenAlex 全学科文献库，含期刊论文和预印本）。"
                "用户想找某个主题的论文、文献、参考文献，问某方向的研究进展或经典论文，"
                "或正在写论文/做研究需要查资料时使用。"
                "query 传核心学术关键词（中英文均可），不要口语化；"
                "结果返回标题、作者、年份、期刊、被引数、url（DOI 或 OpenAlex 网址）"
                "与摘要，均为真实元数据。整理回复时每篇文献必须附上其 url。"
                "普通资讯、新闻仍用 search_web。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "检索关键词，中文或英文学术用语均可，"
                            "例如「图神经网络 综述」「knowledge graph embedding survey」。"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回论文数量，默认 5，最多 8。",
                        "minimum": 1,
                        "maximum": 8,
                    },
                    "sort": {
                        "type": "string",
                        "description": (
                            "排序方式：relevance=按相关度（默认）；"
                            "cited=按被引量降序（找经典/高影响力文献时用）；"
                            "newest=按发表时间降序（找最新进展时用）。"
                        ),
                        "enum": ["relevance", "newest", "cited"],
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


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



