"""屏幕视觉理解：复用 TOOLBOX_VISION_* 既有配置的 openai-compat 通道。

单帧描述（「在干嘛」问答）与多帧拼图描述（围观评论）共用一套请求内核：
- describe_screen：一帧 → 旁观者状态描述；
- describe_screen_sheet：一段时间内的多帧拼成一张网格图（序号 + 相对
  时间烧录在每帧左上角）→ 一次调用产出「这段时间发生了什么」的过程描述，
  替代「逐帧 120 字描述再让聊天模型拼过程」的文字瓶颈。拼图像素量与
  单帧全宽截图同级，视觉调用成本不变，信息量随帧数放大。

思考型模型（如 deepseek-flash）需要给足 max_tokens——reasoning 与正文
共用输出配额，上限过小会把正文截成空串（实测 300 不足、3000 稳定）。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

LOGGER = logging.getLogger("atri.screen_watch")

MAX_TOKENS = 3000

# 拼图规格：每帧 640 宽、最多 4 帧排 2×2 网格——整张拼图像素量与单帧
# 全宽截图（1280 宽）同级，一次视觉调用的成本不变。
SHEET_FRAME_WIDTH = 640
SHEET_MAX_FRAMES = 4
SHEET_GRID_COLUMNS = 2
SHEET_GAP = 6
SHEET_JPEG_QUALITY = 82

_SCREEN_PROMPT = (
    "你是屏幕旁观助手。请用中文简洁描述这张屏幕截图里的人正在做什么、"
    "进行到什么状态：玩什么游戏/做什么事、局势或进度如何"
    "（例如打到哪、顺不顺、卡在哪、画面里发生了什么值得反应的事）。"
    "描述供朋友坐在旁边围观吐槽用，重点是有没有可反应的点。"
    "忽略帧率、CPU/内存占用等性能监控数据、硬件悬浮窗与系统状态栏。"
    "控制在 120 字以内，只描述看得清的内容，绝不编造看不清的细节。"
)

_SCREEN_SHEET_PROMPT = (
    "你是屏幕旁观助手。这张网格图是同一块屏幕按时间顺序排列的几帧快照："
    "从左上到右下、从早到近，每帧左上角标注了序号和相对时间（now=当前）。"
    "请用中文描述这段时间里对方在做什么、事态如何变化——"
    "进展、卡关、翻车、翻盘、局势起伏，以及值得旁观者反应的点。"
    "描述供朋友坐在旁边围观吐槽用，重点是这段时间的过程，不是单帧复述。"
    "忽略帧率、CPU/内存占用等性能监控数据、硬件悬浮窗与系统状态栏。"
    "控制在 150 字以内，只描述看得清的内容，绝不编造看不清的细节。"
)


def _label_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        # PIL ≥ 10.1 支持给默认字体指定字号；旧版本回退到位图小字。
        return ImageFont.load_default(size=18)
    except TypeError:
        return ImageFont.load_default()


def _frame_label(index: int, age_seconds: float) -> str:
    """拼图帧角标：序号 + 相对时间。默认字体不含中文，全用 ASCII。"""

    age = max(0.0, float(age_seconds))
    if age <= 1.0:
        part = "now"
    elif age < 60.0:
        part = f"-{int(age)}s"
    elif age < 3600.0:
        part = f"-{int(round(age / 60.0))}m"
    else:
        part = f"-{int(round(age / 3600.0))}h"
    return f"#{index} {part}"


def compose_sheet(frames: list[tuple[float, bytes]], now: float) -> bytes | None:
    """把（时间戳, JPEG）帧序列拼成带角标的网格图。

    任何一帧解码失败都返回 None，由调用方退回当前帧的单帧描述——
    绝不发出缺帧的残缺拼图误导模型。
    """

    if not 2 <= len(frames) <= SHEET_MAX_FRAMES:
        return None
    decoded: list[Image.Image] = []
    for _, jpeg in frames:
        try:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception as exc:
            LOGGER.warning("拼图帧解码失败，整图退回单帧描述：%s", exc)
            return None
        if image.width > SHEET_FRAME_WIDTH:
            # 像素预算在合成层强制执行：即使调用方送来全宽帧，
            # 拼图总量也必须与单帧全宽截图同级，不让成本失控。
            ratio = SHEET_FRAME_WIDTH / image.width
            image = image.resize(
                (SHEET_FRAME_WIDTH, max(1, int(image.height * ratio))), Image.LANCZOS
            )
        decoded.append(image)
    cell_w = max(image.width for image in decoded)
    cell_h = max(image.height for image in decoded)
    rows = (len(decoded) + SHEET_GRID_COLUMNS - 1) // SHEET_GRID_COLUMNS
    sheet = Image.new(
        "RGB",
        (
            cell_w * SHEET_GRID_COLUMNS + SHEET_GAP * (SHEET_GRID_COLUMNS + 1),
            cell_h * rows + SHEET_GAP * (rows + 1),
        ),
        (0, 0, 0),
    )
    draw = ImageDraw.Draw(sheet)
    font = _label_font()
    for position, image in enumerate(decoded):
        row, column = divmod(position, SHEET_GRID_COLUMNS)
        x = SHEET_GAP + column * (cell_w + SHEET_GAP)
        y = SHEET_GAP + row * (cell_h + SHEET_GAP)
        if image.size != (cell_w, cell_h):
            image = image.resize((cell_w, cell_h))
        sheet.paste(image, (x, y))
        label = _frame_label(position + 1, now - frames[position][0])
        # 黑底白字角标贴在每帧左上角，任何画面下都读得清。
        box = draw.textbbox((x, y), label, font=font)
        draw.rectangle(
            (x, y, x + (box[2] - box[0]) + 8, y + (box[3] - box[1]) + 6),
            fill=(0, 0, 0),
        )
        draw.text((x + 4, y + 3), label, fill=(255, 255, 255), font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=SHEET_JPEG_QUALITY)
    return buffer.getvalue()


def _describe_image(prompt: str, jpeg: bytes, config) -> str:
    """请求内核：一张图 + 一段提示词 → 视觉模型文本；失败返回空串。"""

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
                        {"type": "text", "text": prompt},
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


def describe_screen(jpeg: bytes, config) -> str:
    """把单张屏幕截图送入视觉模型，返回中文描述；失败返回空串（不编造）。"""

    return _describe_image(_SCREEN_PROMPT, jpeg, config)


def describe_screen_sheet(frames: list[tuple[float, bytes]], config) -> str:
    """多帧 → 过程描述：≥2 帧拼图直传视觉模型，一次看到一段时间。

    单帧（围观刚开始）退回单帧描述；拼图失败退回最新一帧的
    单帧描述——围观评论在任何降级下都不中断。
    """

    if not frames:
        return ""
    trimmed = frames[-SHEET_MAX_FRAMES:]
    if len(trimmed) == 1:
        return describe_screen(trimmed[0][1], config)
    # 角标的相对时间以最新一帧为基准，无需依赖墙钟，行为可测试。
    try:
        sheet = compose_sheet(trimmed, trimmed[-1][0])
    except Exception as exc:
        # 合成异常（解码之外的新 PIL 路径）不冒泡：围观链路任何故障
        # 都只降级、不中断，与 describe_screen 的契约一致。
        LOGGER.warning("拼图合成异常，退回单帧描述：%s", exc)
        sheet = None
    if sheet is None:
        return describe_screen(trimmed[-1][1], config)
    return _describe_image(_SCREEN_SHEET_PROMPT, sheet, config)
