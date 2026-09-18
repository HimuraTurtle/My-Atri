"""B站视频解析与 AI 总结（移植自 AstrBot biliVideo 插件）。

丢一个 B站链接，ATRI 帮你秒出精华总结：
- 命令入口：B站总结 <链接/BV号>、B站解析 <链接/BV号>、B站帮助
- 自动总结：开启 BILIBILI_AUTO_SUMMARY 后，纯链接/分享卡片消息自动触发
- 字幕优先，无字幕视频走 DASH 音频下载 + 必剪 ASR 转写
- 总结结果按 BV 号缓存 6 小时
"""
from .client import (
    BilibiliApiClient,
    BilibiliError,
    BilibiliNetworkError,
    SubtitleTrack,
    VideoInfo,
)
from .parsing import extract_bvid, extract_long_url, extract_short_url
from .service import (
    BilibiliCommandResult,
    BilibiliService,
    get_bilibili_service,
)
from .subtitles import Transcript, TranscriptSegment
from .summarize import BilibiliSummarizer

__all__ = [
    "BilibiliApiClient",
    "BilibiliCommandResult",
    "BilibiliError",
    "BilibiliNetworkError",
    "BilibiliService",
    "BilibiliSummarizer",
    "SubtitleTrack",
    "Transcript",
    "TranscriptSegment",
    "VideoInfo",
    "extract_bvid",
    "extract_long_url",
    "extract_short_url",
    "get_bilibili_service",
]
