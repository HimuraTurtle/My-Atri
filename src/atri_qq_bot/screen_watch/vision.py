"""屏幕视觉理解：复用 TOOLBOX_VISION_* 既有配置的 openai-compat 通道。

思考型模型（如 deepseek-flash）需要给足 max_tokens——reasoning 与正文
共用输出配额，上限过小会把正文截成空串（实测 300 不足、3000 稳定）。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request

LOGGER = logging.getLogger("atri.screen_watch")

MAX_TOKENS = 3000

_SCREEN_PROMPT = (
    "你是桌面屏幕理解助手。请用中文简洁描述这张屏幕截图："
    "用户主要在做什么、前台应用或网站是什么（尽量识别窗口标题、"
    "地址栏、页面文字等可见证据）。控制在 120 字以内，"
    "只描述看得清的内容，绝不编造看不清的细节。"
)


def describe_screen(jpeg: bytes, config) -> str:
    """把屏幕截图送入视觉模型，返回中文描述；失败返回空串（不编造）。"""
    base_url = str(getattr(config, "toolbox_vision_base_url", "") or "").rstrip("/")
    model = str(getattr(config, "toolbox_vision_model", "") or "")
    api_key = str(getattr(config, "toolbox_vision_api_key", "") or "")
    if not (base_url and model and api_key):
        LOGGER.warning("屏幕视觉配置不完整：base_url/model/api_key 存在空值，跳过描述")
        return ""
    timeout = float(getattr(config, "toolbox_vision_timeout_seconds", 55.0) or 55.0)
    retries = max(0, int(getattr(config, "toolbox_vision_retry_count", 1) or 0))

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _SCREEN_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + base64.b64encode(jpeg).decode()
                            },
                        },
                    ],
                }
            ],
            "max_tokens": MAX_TOKENS,
        }
    ).encode()

    last_error = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            content = str(
                payload.get("choices", [{}])[0].get("message", {}).get("content") or ""
            ).strip()
            if content:
                return content
            last_error = "模型返回空正文（可能被 max_tokens 截断）"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc)
        LOGGER.warning(
            "屏幕描述第 %d 次失败：%s", attempt + 1, last_error or "未知错误"
        )
        if attempt < retries:
            time.sleep(1.5)
    return ""
