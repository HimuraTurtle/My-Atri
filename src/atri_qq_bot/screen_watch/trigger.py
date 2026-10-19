"""帧差分触发器：OpenCV 判定「画面变了」，视觉模型只在语义变化点调用。

状态机要点：
- 连续 confirm_frames 帧超阈值才触发（滤掉弹窗闪烁、光标扫动）；
- 触发后进入冷却，冷却内变化不再调用模型（视频/游戏画面会持续
  触发差分，但语义上「在打游戏」不需要每两分钟重新描述一次）；
- 高频触发时指数退避冷却，长时间静止后自动恢复基础冷却。
"""

from __future__ import annotations

import time

import cv2
import numpy as np


class FrameTrigger:
    def __init__(
        self,
        threshold: float = 8.0,
        confirm_frames: int = 2,
        cooldown: float = 120.0,
        backoff_max: float = 300.0,
    ) -> None:
        self.threshold = max(0.5, float(threshold))
        self.confirm_frames = max(1, int(confirm_frames))
        self.base_cooldown = max(10.0, float(cooldown))
        self.backoff_max = max(self.base_cooldown, float(backoff_max))
        self._prev: np.ndarray | None = None
        self._streak = 0
        self._last_fire = 0.0
        self._cooldown = self.base_cooldown

    @property
    def current_cooldown(self) -> float:
        return self._cooldown

    def feed(self, gray: np.ndarray, now: float | None = None) -> bool:
        """喂入一张差分用灰度小图；返回 True 表示应立即调用视觉模型。"""
        now = time.time() if now is None else now
        prev = self._prev
        self._prev = gray
        if prev is None or prev.shape != gray.shape:
            self._streak = 0
            return False
        score = float(cv2.absdiff(gray, prev).mean())
        if score < self.threshold:
            self._streak = 0
            self._maybe_recover(now)
            return False
        self._streak += 1
        if self._streak < self.confirm_frames:
            return False
        self._streak = 0
        if now - self._last_fire < self._cooldown:
            self._maybe_recover(now)
            return False
        self._last_fire = now
        return True

    def reset_baseline(self) -> None:
        """触发成功后清除基线：下一帧作为新场景起点，避免连环误触发。"""
        self._prev = None
        self._streak = 0

    def note_busy(self, now: float | None = None) -> None:
        """短时间多次触发（视频/游戏）时指数退避，保护 API 配额。"""
        now = time.time() if now is None else now
        self._cooldown = min(self._cooldown * 1.5, self.backoff_max)
        if now - self._last_fire < self._cooldown:
            pass

    def _maybe_recover(self, now: float) -> None:
        if self._last_fire > 0.0 and now - self._last_fire >= self.backoff_max:
            self._cooldown = self.base_cooldown
