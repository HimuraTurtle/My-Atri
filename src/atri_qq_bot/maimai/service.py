"""舞萌 DX 查分器（落雪）：为聊天模型提供 B50 / AP50 查询能力。

移植自 AstrBot astrbot_plugin_maimaidx 的落雪开发者模式最小路径：
管理员在落雪查分器（maimai.lxns.net）申请开发者 Token 后，用户只需
在落雪官网绑定 QQ 号并开放第三方读取，即可按 QQ 号直查，无需用户侧
OAuth 授权或本地绑定。

契约（与 papers 模块一致）：纯只读 HTTP 查询，无状态、无本地缓存；
失败或无结果时返回明确的中文错误说明，绝不编造成绩。

省略项：原插件的重资产能力（曲库合并、B50 绘图、机厅/猜歌/牌子）
不引入；开发者接口个别成绩只给评级（rate）不给精确达成率时，按
评级下界估算并在结果里标记 achievement_approx。段位/阶级 ID 没有
权威中文对照表，保留原始数值交由模型如实转述，不翻译。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

LOGGER = logging.getLogger("atri.maimai")

_LXNS_DEV_BASE = "https://maimai.lxns.net/api/v0/maimai"
_MAX_TOP_SCORES = 10

_DIFFICULTY_NAMES = ("BASIC", "ADVANCED", "EXPERT", "MASTER", "Re:MASTER")
_FC_NAMES = {"fc": "FC", "fcp": "FC+", "ap": "AP", "app": "AP+"}
_FS_NAMES = {"fs": "FS", "fsp": "FS+", "fdx": "FDX", "fdxp": "FDX+"}
# 评级 -> 达成率下界：开发者接口个别成绩缺失精确达成率时用于估算。
_RATE_TO_ACHIEVEMENT = {
    "sssp": 100.5,
    "sss": 100.0,
    "ssp": 99.5,
    "ss": 99.0,
    "sp": 98.0,
    "s": 97.0,
    "aaa": 94.0,
    "aa": 90.0,
    "a": 80.0,
    "bbb": 75.0,
    "bb": 70.0,
    "b": 60.0,
    "c": 50.0,
    "d": 0.0,
}


class _LxnsApiError(Exception):
    """落雪接口错误：message 为可直接转达用户的中文说明。"""


def _difficulty_name(level_index: Any) -> str:
    try:
        index = int(level_index)
    except (TypeError, ValueError):
        return ""
    if 0 <= index < len(_DIFFICULTY_NAMES):
        return _DIFFICULTY_NAMES[index]
    return f"难度{index}"


def _to_score_item(score: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    achievements = score.get("achievements")
    approx = achievements is None
    if approx:
        achievements = _RATE_TO_ACHIEVEMENT.get(
            str(score.get("rate") or "").strip().lower(), 0.0
        )
    try:
        achievements = round(float(achievements), 4)
    except (TypeError, ValueError):
        achievements = 0.0
        approx = True
    try:
        rating = int(float(score.get("dx_rating") or 0))
    except (TypeError, ValueError):
        rating = 0
    try:
        dx_score = int(score.get("dx_score") or 0)
    except (TypeError, ValueError):
        dx_score = 0
    try:
        dx_star = int(score.get("dx_star") or 0)
    except (TypeError, ValueError):
        dx_star = 0
    return (
        {
            "title": str(score.get("song_name") or "").strip()
            or f"曲目#{score.get('id')}",
            "difficulty": _difficulty_name(score.get("level_index")),
            "level": str(score.get("level") or ""),
            "chart": "DX谱" if str(score.get("type") or "") == "dx" else "标准谱",
            "achievement": achievements,
            "achievement_approx": approx,
            "rate": str(score.get("rate") or ""),
            "fc": _FC_NAMES.get(str(score.get("fc") or "").strip().lower(), ""),
            "fs": _FS_NAMES.get(str(score.get("fs") or "").strip().lower(), ""),
            "dx_score": dx_score,
            "dx_star": dx_star,
            "rating": rating,
        },
        approx,
    )


class MaimaiService:
    """落雪舞萌查分器：纯只读 HTTP 查询，无状态、无本地缓存。"""

    def __init__(self, config: Any) -> None:
        self._config = config

    async def best50_by_qq(self, qq: int, mode: str = "b50") -> dict[str, Any]:
        try:
            qq = int(qq)
        except (TypeError, ValueError):
            return {"ok": False, "error": "QQ 号必须是数字"}
        if not (10_000 <= qq <= 99_999_999_999):
            return {"ok": False, "error": "QQ 号格式不正确"}
        token = str(getattr(self._config, "maimai_lxns_dev_token", "") or "").strip()
        if not token:
            return {
                "ok": False,
                "error": "舞萌查分器未配置落雪开发者 Token（MAIMAI_LXNS_DEV_TOKEN）",
            }
        ap = str(mode or "").strip().lower() == "ap50"
        headers = {"Authorization": token}
        timeout = max(
            5.0,
            min(
                60.0,
                float(getattr(self._config, "maimai_timeout_seconds", 20.0) or 20.0),
            ),
        )
        try:
            player = await self._fetch(f"/player/qq/{qq}", headers, timeout)
            friend_code = int(player.get("friend_code") or 0)
            if not friend_code:
                return {"ok": False, "error": "落雪未返回该玩家的好友码"}
            path = f"/player/{friend_code}/bests" + ("/ap" if ap else "")
            bests = await self._fetch(path, headers, timeout)
        except _LxnsApiError as exc:
            LOGGER.warning("舞萌查分失败（qq=%s）：%s", qq, exc)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            LOGGER.warning("舞萌查分失败（qq=%s）：%s", qq, exc)
            return {"ok": False, "error": f"落雪接口调用失败：{exc}"}
        LOGGER.info(
            "舞萌查分完成：qq=%s mode=%s rating=%s",
            qq,
            "ap50" if ap else "b50",
            player.get("rating"),
        )
        return self._summarize(qq, "ap50" if ap else "b50", player, bests)

    def _summarize(
        self,
        qq: int,
        mode: str,
        player: dict[str, Any],
        bests: dict[str, Any],
    ) -> dict[str, Any]:
        standard = [
            item
            for item in (bests.get("standard") or [])
            if isinstance(item, dict)
        ]
        dx = [item for item in (bests.get("dx") or []) if isinstance(item, dict)]
        merged = sorted(
            [*standard, *dx],
            key=lambda item: float(item.get("dx_rating") or 0),
            reverse=True,
        )
        top_scores: list[dict[str, Any]] = []
        has_approx = False
        for score in merged[:_MAX_TOP_SCORES]:
            item, approx = _to_score_item(score)
            has_approx = has_approx or approx
            top_scores.append(item)
        trophy = player.get("trophy")
        notes = ["course_rank/class_rank 为游戏内部数字代码，没有把握不要翻译成段位名"]
        if has_approx:
            notes.append("achievement_approx=true 的达成率为按评级下界估算，非精确值")
        return {
            "ok": True,
            "mode": mode,
            "qq": qq,
            "player": {
                "name": str(player.get("name") or "").strip(),
                "rating": int(player.get("rating") or 0),
                "friend_code": int(player.get("friend_code") or 0),
                "course_rank": int(player.get("course_rank") or 0),
                "class_rank": int(player.get("class_rank") or 0),
                "star": int(player.get("star") or 0),
                "trophy": str(trophy.get("name") or "").strip()
                if isinstance(trophy, dict)
                else "",
                "upload_time": str(player.get("upload_time") or ""),
            },
            "standard_total": int(bests.get("standard_total") or 0),
            "dx_total": int(bests.get("dx_total") or 0),
            "standard_count": len(standard),
            "dx_count": len(dx),
            "top_scores": top_scores,
            "note": "；".join(notes),
        }

    async def _fetch(
        self, path: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{_LXNS_DEV_BASE}{path}", headers=headers)
        if response.status_code == 404:
            raise _LxnsApiError(
                "该 QQ 号未在落雪查分器绑定；需要玩家先在 maimai.lxns.net "
                "绑定 QQ 号，并在账号设置的隐私设置中允许第三方读取成绩"
            )
        if response.status_code == 403:
            raise _LxnsApiError(
                "该玩家在落雪的隐私设置未开放第三方读取成绩，需要其到"
                " maimai.lxns.net 的隐私设置中开启"
            )
        if response.status_code == 401:
            raise _LxnsApiError("落雪开发者 Token 无效或已过期，需要管理员重新配置")
        if response.status_code == 429:
            raise _LxnsApiError("落雪接口请求过于频繁，请稍后再试")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise _LxnsApiError("落雪接口返回了无法解析的数据") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise _LxnsApiError("落雪接口返回数据为空")
        return data
