"""人级别身份别名注册表：稳定 QQ 号 -> 规范名 + 曾用名集合。

背景（2026-11 对话实测与上游 livingmemory 2.5.1 对照）：记忆条目的归属
此前只存瞬时昵称文本，群友一改名，同一个人在记忆里裂成两个「人」，
是「认错人」的主要来源之一。上游用 identity_aliases（platform:id 为
键、昵称作别名）解决；本模块是它在 ATRI 的人级别落地：

- ``record``：每条观察到的消息昵称自动登记，最新昵称即 current；
- ``set_preferred``：用户亲口教的称呼（「叫我 XX」）优先于一切昵称；
- ``display``：渲染时的当前称呼（preferred > current）；
- ``former_names``：曾用名列表，注入「当前发言的群友：YY（曾用名…）」
  锚点，帮助模型把记忆里的旧昵称与眼前的人对上号。

存储为单个 JSON 文件（与 users.json 同目录），进程内 RLock 保护，
WebUI/测试可直接构造。容量有上限：人数超限时按 updated_at 淘汰最旧。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("atri.memory.alias")

_ALIASES_CAP = 12  # 每人保留的曾用名上限（含 current）
_PEOPLE_CAP = 4096  # 注册表人数上限；超限淘汰最久未活跃
_VERSION = 1


class AliasRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._people: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------------- 持久化 ----------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            people = data.get("people") if isinstance(data, dict) else None
            if isinstance(people, dict):
                self._people = {
                    str(key): value
                    for key, value in people.items()
                    if isinstance(value, dict)
                }
        except FileNotFoundError:
            self._people = {}
        except (OSError, ValueError):
            LOGGER.warning("身份别名注册表损坏，重建：%s", self.path)
            self._people = {}

    def _save(self) -> None:
        payload = {"version": _VERSION, "people": self._people}
        tmp = self.path.with_name(f".{self.path.name}.{time.time_ns():x}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            LOGGER.warning("身份别名注册表保存失败：%s", self.path, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _evict_if_crowded(self) -> None:
        if len(self._people) <= _PEOPLE_CAP:
            return
        ordered = sorted(
            self._people.items(),
            key=lambda item: float(item[1].get("updated_at") or 0.0),
        )
        for key, _ in ordered[: len(self._people) - _PEOPLE_CAP]:
            self._people.pop(key, None)

    # ---------------- 写入 ----------------

    def record(self, actor_id: Any, name: str) -> bool:
        """登记观察到的昵称；昵称变化会推进 current 并把旧名收进 aliases。"""
        key = str(actor_id or "").strip()
        nickname = str(name or "").strip()
        if not key or not nickname or nickname.isdigit() and len(nickname) >= 6:
            # 纯数字长串多半是没拿到群名片时的 QQ 号回显，不是称呼。
            return False
        with self._lock:
            person = self._people.get(key)
            if person is None:
                person = {"current": nickname, "preferred": "", "aliases": [nickname], "updated_at": 0.0}
                self._people[key] = person
            changed = False
            if person.get("current") != nickname:
                person["current"] = nickname
                changed = True
            aliases = [str(a) for a in person.get("aliases") or [] if str(a).strip()]
            if nickname not in aliases:
                aliases.append(nickname)
                changed = True
            person["aliases"] = aliases[-_ALIASES_CAP:]
            person["updated_at"] = time.time()
            self._evict_if_crowded()
            if changed:
                self._save()
            return changed

    def set_preferred(self, actor_id: Any, name: str) -> bool:
        """记录用户亲口教的称呼（「叫我 XX」）；清空用空串。"""
        key = str(actor_id or "").strip()
        preferred = str(name or "").strip()
        if not key or not preferred or preferred.isdigit():
            return False
        with self._lock:
            person = self._people.get(key)
            if person is None:
                person = {"current": "", "preferred": preferred, "aliases": [], "updated_at": 0.0}
                self._people[key] = person
            if person.get("preferred") == preferred:
                return False
            person["preferred"] = preferred
            aliases = [str(a) for a in person.get("aliases") or [] if str(a).strip()]
            if preferred not in aliases:
                aliases.append(preferred)
            person["aliases"] = aliases[-_ALIASES_CAP:]
            person["updated_at"] = time.time()
            self._save()
            return True

    # ---------------- 读取 ----------------

    def display(self, actor_id: Any) -> str:
        """当前称呼：用户教的称呼优先，其次最新昵称。"""
        key = str(actor_id or "").strip()
        if not key:
            return ""
        with self._lock:
            person = self._people.get(key)
            if not person:
                return ""
            return str(person.get("preferred") or person.get("current") or "").strip()

    def former_names(self, actor_id: Any, limit: int = 3) -> list[str]:
        """曾用名（不含当前 display），用于注入对照锚点。"""
        key = str(actor_id or "").strip()
        if not key:
            return []
        with self._lock:
            person = self._people.get(key)
            if not person:
                return []
            current = str(person.get("preferred") or person.get("current") or "").strip()
            aliases = [str(a).strip() for a in person.get("aliases") or [] if str(a).strip()]
            return [name for name in aliases if name != current][:limit]

    def resolve_display_name(
        self, actor_id: Any, remembered_name: str
    ) -> str:
        """渲染解析：按 QQ 号取当前称呼；注册表没见过时回退记忆里的旧名。"""
        resolved = self.display(actor_id)
        return resolved or str(remembered_name or "").strip()

    def known(self, actor_id: Any) -> bool:
        return str(actor_id or "").strip() in self._people

    def snapshot(self) -> dict[str, Any]:
        """WebUI/诊断只读快照。"""
        with self._lock:
            return {
                "people": len(self._people),
                "sample": [
                    {"id": key, "display": self.display(key), "aliases": person.get("aliases")}
                    for key, person in list(self._people.items())[:8]
                ],
            }


def build_display_resolver(registry: AliasRegistry) -> Callable[[Any, str], str]:
    """给检索层用的轻量解析函数（避免 planner 直接持有注册表对象）。"""

    def resolver(actor_id: Any, remembered_name: str) -> str:
        return registry.resolve_display_name(actor_id, remembered_name)

    return resolver
