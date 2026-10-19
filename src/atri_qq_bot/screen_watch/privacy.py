"""隐私闸：敏感窗口跳过 + 文件级急停。

- 前台窗口标题命中敏感关键词时不截图入模型，摘要静默丢弃；
- data/screen_watch/PAUSED 文件存在即全量暂停（急停），
  用户可在 QQ 指令或直接删建该文件间切换，无需重启进程。
"""

from __future__ import annotations

import ctypes
from pathlib import Path

SENSITIVE_KEYWORDS = (
    "银行",
    "网银",
    "支付",
    "付款",
    "密码",
    "登录",
    "登陆",
    "账单",
    "信用卡",
    "钱包",
    "身份证",
    "银行卡",
    "验证码",
    "转账",
    "余额",
    "隐身",
    "隐私",
    "private",
    "incognito",
    "inprivate",
    "banking",
    "payment",
    "password",
    "login",
    "signin",
    "sign in",
    "sign-in",
    "checkout",
)

_USER32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def active_window_title() -> str:
    """读取当前前台窗口标题；无界面环境或失败时返回空串。"""
    if _USER32 is None:
        return ""
    try:
        hwnd = _USER32.GetForegroundWindow()
        if not hwnd:
            return ""
        buffer = ctypes.create_unicode_buffer(512)
        length = _USER32.GetWindowTextW(hwnd, buffer, 512)
        return buffer.value[:length] if length > 0 else ""
    except Exception:
        return ""


def is_sensitive_title(title: str) -> bool:
    lowered = str(title or "").strip().lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in SENSITIVE_KEYWORDS)


class PauseFlag:
    """以标志文件实现的急停开关：存在即暂停。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def is_set(self) -> bool:
        return self.path.exists()

    def set(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("paused\n", encoding="utf-8")

    def clear(self) -> None:
        with_context = self.path
        if with_context.exists():
            with_context.unlink()
