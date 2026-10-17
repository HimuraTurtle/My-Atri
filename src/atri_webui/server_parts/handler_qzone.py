from __future__ import annotations

import threading
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from ..qzone_admin import QZoneAdmin, QZoneBridgeError
from ..qzone_automation import parse_keywords
from .shared import LOGGER, generate_qzone_text, run_coro


class HandlerQZoneMixin:
    def _qzone_admin(self) -> QZoneAdmin:
        # 复用记忆面板的聚合结果，确保这里只能选择已经认识的人物 QQ。
        return QZoneAdmin(
            self.webui_state.memory_admin.summary(),
            bot_qq=self.webui_state.config.bot_qq,
            owner_qqs=self.webui_state.config.owner_qqs,
        )

    def _handle_qzone_dashboard(self) -> None:
        self._send_json(self._qzone_admin().dashboard())

    def _handle_qzone_friends(self) -> None:
        try:
            self._send_json({"ok": True, "friends": self._qzone_admin().friends()})
        except (ValueError, QZoneBridgeError) as exc:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))

    def _handle_qzone_automation_settings(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        values = dict(payload)
        for key in ("like_keywords", "comment_keywords"):
            if key in values:
                values[key] = parse_keywords(values[key])
        for key in ("enabled", "post_enabled", "post_auto_generate", "like_enabled", "comment_enabled", "comment_use_pool", "devlog_enabled", "devlog_auto_publish"):
            if key in values:
                values[key] = bool(values[key])
        if "devlog_daily_cap" in values:
            try:
                values["devlog_daily_cap"] = max(0, min(10, int(values["devlog_daily_cap"])))
            except (TypeError, ValueError):
                return self._send_error(HTTPStatus.BAD_REQUEST, "devlog_daily_cap 必须是整数")
        if "devlog_visibility_types" in values:
            raw_types = values["devlog_visibility_types"]
            if isinstance(raw_types, str):
                raw_types = raw_types.replace("，", ",").split(",")
            values["devlog_visibility_types"] = [str(item).strip().casefold() for item in (raw_types or []) if str(item).strip()]
        # Legacy fixed/random fields remain readable for migration, but no
        # longer participate in scheduling. There is one supported model:
        # stable random slots distributed inside the configured window.
        values["post_mode"] = "window"
        for key, lower, upper in (("period_days", 1, 31), ("posts_per_period", 1, 20), ("post_start_hour", 0, 23), ("post_end_hour", 1, 24)):
            if key in values:
                try:
                    values[key] = max(lower, min(upper, int(values[key])))
                except (TypeError, ValueError):
                    return self._send_error(HTTPStatus.BAD_REQUEST, f"{key} 必须是整数")
        if "topic_rotation" in values:
            values["topic_rotation"] = parse_keywords(values["topic_rotation"])
        for key, upper in (("feed_limit", 50), ("max_likes_per_run", 50), ("max_comments_per_run", 20)):
            if key in values:
                try:
                    values[key] = max(0 if key != "feed_limit" else 1, min(upper, int(values[key])))
                except (TypeError, ValueError):
                    return self._send_error(HTTPStatus.BAD_REQUEST, f"{key} 必须是整数")
        if "visibility" in values:
            try:
                values["visibility"] = int(values["visibility"])
            except (TypeError, ValueError):
                return self._send_error(HTTPStatus.BAD_REQUEST, "可见范围无效")
            if values["visibility"] not in {0, 1, 2}:
                return self._send_error(HTTPStatus.BAD_REQUEST, "可见范围无效")
        try:
            settings = self.webui_state.qzone_automation.store.save_settings(values)
        except (ValueError, TypeError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True, "settings": settings})

    def _handle_qzone_automation_content(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            item = self.webui_state.qzone_automation.store.content_add(str(payload.get("content") or ""), prompt=str(payload.get("prompt") or ""), model_profile_id=str(payload.get("model_profile_id") or ""), generated_at=float(payload.get("generated_at") or 0) or None)
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True, "item": item})

    def _handle_qzone_automation_content_update(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少内容 id")
        try:
            self.webui_state.qzone_automation.store.content_update(int(payload["id"]), str(payload.get("content") or ""), str(payload.get("status") or "") or None, prompt=payload.get("prompt"), model_profile_id=payload.get("model_profile_id"))
        except (ValueError, TypeError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True})

    def _handle_qzone_automation_content_delete(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少内容 id")
        self.webui_state.qzone_automation.store.content_delete(int(payload["id"]))
        # Deleting the visible pending item must immediately create/reveal the
        # next scheduled item; waiting for the 30-second scheduler made the UI
        # look empty until the next refresh.
        try:
            item = self.webui_state.qzone_automation.refresh_next_post()
        except Exception as exc:
            LOGGER.exception("QQ 空间删除动态后重排失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json({"ok": True, "item": item})

    def _handle_qzone_automation_targets(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not isinstance(payload.get("targets"), list):
            return self._send_error(HTTPStatus.BAD_REQUEST, "targets 必须是数组")
        self._send_json({"ok": True, "targets": self.webui_state.qzone_automation.store.save_targets(payload["targets"])})

    def _handle_qzone_automation_run(self) -> None:
        # 这是明确的人工触发，仍受总控和模块开关约束。
        try:
            result = self.webui_state.qzone_automation.run_once()
        except Exception as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_publish_generated(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            result = self.webui_state.qzone_automation.publish_generated_now(
                str(payload.get("prompt") or ""),
                str(payload.get("model_profile_id") or ""),
            )
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            LOGGER.exception("QQ 空间即时生成发布失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_prepare_next(self) -> None:
        try:
            item = self.webui_state.qzone_automation.prepare_next_generated()
        except Exception as exc:
            LOGGER.exception("QQ 空间下一条动态预生成失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json({"ok": True, "item": item})

    def _handle_qzone_automation_publish(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少内容 id")
        try:
            result = self.webui_state.qzone_automation.publish_now(int(payload["id"]))
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_generate(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            result = run_coro(generate_qzone_text(self.webui_state.config, str(payload.get("prompt") or ""), str(payload.get("kind") or "post"), str(payload.get("profile_id") or "")))
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            LOGGER.exception("QQ 空间文案生成失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_comment(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            item = self.webui_state.qzone_automation.store.comment_add(str(payload.get("content") or ""), prompt=str(payload.get("prompt") or ""), model_profile_id=str(payload.get("model_profile_id") or ""), source_tid=str(payload.get("source_tid") or ""), source_user_id=str(payload.get("source_user_id") or ""), source_content=str(payload.get("source_content") or ""), generated_at=float(payload.get("generated_at") or 0) or None)
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True, "item": item})

    def _handle_qzone_automation_comment_update(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少评论 id")
        try:
            self.webui_state.qzone_automation.store.comment_update(int(payload["id"]), str(payload.get("content") or ""), str(payload.get("status") or "") or None, prompt=payload.get("prompt"), model_profile_id=payload.get("model_profile_id"), source_tid=payload.get("source_tid"), source_user_id=payload.get("source_user_id"), source_content=payload.get("source_content"))
        except (ValueError, TypeError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True})

    def _handle_qzone_automation_comment_delete(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少评论 id")
        self.webui_state.qzone_automation.store.comment_delete(int(payload["id"]))
        self._send_json({"ok": True})

    # ---- 开发日志（devlog）：状态、采集、生成、草稿、发布、删除 ----

    def _handle_qzone_automation_devlog(self) -> None:
        self._send_json(self.webui_state.qzone_automation.devlog.status())

    def _handle_qzone_automation_devlog_scan(self) -> None:
        # 明确的人工触发：绕过采集节流，只入库提交，不发布任何内容。
        try:
            result = self.webui_state.qzone_automation.devlog.scan(force=True)
        except Exception as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_devlog_generate(self) -> None:
        payload = self._read_json()
        regenerate = bool(isinstance(payload, dict) and payload.get("regenerate"))
        devlog = self.webui_state.qzone_automation.devlog
        if regenerate:
            draft = devlog.store.devlog_draft()
            if draft is not None:
                devlog.delete_draft(int(draft["id"]))
        try:
            result = devlog.generate(force=True)
        except Exception as exc:
            LOGGER.exception("开发手记生成失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_devlog_draft(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少手记 id")
        try:
            self.webui_state.qzone_automation.devlog.store.devlog_update_post_content(int(payload["id"]), str(payload.get("content") or ""))
        except (ValueError, TypeError) as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        self._send_json({"ok": True})

    def _handle_qzone_automation_devlog_publish(self) -> None:
        payload = self._read_json()
        post_id: int | None = None
        if isinstance(payload, dict) and payload.get("id"):
            try:
                post_id = int(payload["id"])
            except (TypeError, ValueError):
                return self._send_error(HTTPStatus.BAD_REQUEST, "手记 id 无效")
        # 人工发布仍走幂等状态机与每日上限，不会绕过安全闸。
        try:
            result = self.webui_state.qzone_automation.devlog.publish(post_id=post_id)
        except Exception as exc:
            LOGGER.exception("开发手记发布失败")
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_automation_devlog_delete(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict) or not payload.get("id"):
            return self._send_error(HTTPStatus.BAD_REQUEST, "缺少手记 id")
        self._send_json(self.webui_state.qzone_automation.devlog.delete_draft(int(payload["id"])))

    def _handle_qzone_feeds(self, query: str) -> None:
        params = parse_qs(query)
        try:
            result = self._qzone_admin().feeds(
                params.get("user_id", [""])[0],
                params.get("limit", ["10"])[0],
            )
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except QZoneBridgeError as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_all_feeds(self, query: str) -> None:
        params = parse_qs(query)
        try:
            result = self._qzone_admin().all_feeds(params.get("limit", ["20"])[0])
        except QZoneBridgeError as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_like(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            result = self._qzone_admin().like(payload)
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except QZoneBridgeError as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_comment(self) -> None:
        payload = self._read_json()
        if not isinstance(payload, dict):
            return self._send_error(HTTPStatus.BAD_REQUEST, "invalid json")
        try:
            result = self._qzone_admin().comment(payload)
        except ValueError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except QZoneBridgeError as exc:
            return self._send_error(HTTPStatus.BAD_GATEWAY, str(exc))
        self._send_json(result)

    def _handle_qzone_login(self) -> None:
        """从 QQ 动态面板触发一次 Bridge 可视化登录，不阻塞 8787。"""
        state = self.webui_state
        with state.qzone_login_lock:
            if state.qzone_login_running:
                return self._send_json({"ok": True, "running": True, "message": "QQ 空间登录已在进行中"})
            state.qzone_login_running = True
            state.qzone_login_last = {"ok": None, "message": "正在打开 QQ 空间登录页"}

        def worker() -> None:
            try:
                self._qzone_admin().client.action("login_qr", {})
                state.qzone_login_last = {"ok": True, "message": "QQ 空间登录成功"}
            except Exception as exc:
                state.qzone_login_last = {"ok": False, "message": str(exc)[:300]}
            finally:
                with state.qzone_login_lock:
                    state.qzone_login_running = False

        threading.Thread(target=worker, name="qzone-login", daemon=True).start()
        self._send_json({"ok": True, "running": True, "message": "正在打开 QQ 空间登录页"})

    def _handle_qzone_login_status(self) -> None:
        state = self.webui_state
        with state.qzone_login_lock:
            running = state.qzone_login_running
            last = dict(state.qzone_login_last)
        try:
            bridge = self._qzone_admin().client.status()
            nested = bridge.get("data") if isinstance(bridge.get("data"), dict) else {}
            def status_flag(value: Any) -> bool:
                if isinstance(value, str):
                    return value.strip().lower() not in {"", "0", "false", "no", "off", "未登录", "not_logged_in"}
                return bool(value)
            logged_in = any(
                status_flag(bridge.get(key)) or status_flag(nested.get(key))
                for key in ("logged_in", "is_logged_in", "login", "ok", "qq", "uin")
            )
            self._send_json({
                "ok": True,
                "reachable": True,
                "logged_in": logged_in,
                "qq": bridge.get("qq") or bridge.get("uin") or nested.get("qq") or nested.get("uin"),
                "login_running": running,
                "last": last,
            })
        except QZoneBridgeError as exc:
            self._send_json({
                "ok": True,
                "reachable": False,
                "logged_in": False,
                "login_running": running,
                "last": {**last, "message": str(exc)},
            })


