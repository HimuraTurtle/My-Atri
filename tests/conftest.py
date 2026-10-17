"""全局测试隔离。

未显式传入 memory_path 等路径的 BotConfig 会把默认数据路径解析到真实生产
目录：历史上多次 pytest 运行把 private:10001、group:20001 等测试会话写进
data/memory/users.json，并连带新建了 data/memory/living_memory.sqlite3。
此 autouse fixture 把 schema 里的默认路径根目录重定向到各测试自己的
tmp_path，从根源切断污染；显式传入路径的测试不受影响。

反向搜图（trace.moe / SauceNAO）是真实外网接口：纯白测试图偶发帧级匹配
曾把 visual_status 的断言从 unavailable 随机翻成 verified。这里统一把
反搜入口 mock 成空结果；需要命中的测试在自身内重新 patch 覆盖。
视觉事件日志同样重定向到 tmp_path，避免测试样本混进生产 vision-events。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_data_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("atri_qq_bot.config.schema._project_root", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _hermetic_vision_environment(tmp_path, monkeypatch):
    async def _no_reverse_hits(
        data: bytes, saucenao_key: str = "", animetrace_enabled: bool = True
    ) -> list:
        return []

    async def _no_reverse_findings(data: bytes) -> list:
        return []

    async def _no_tracemoe_like(data: bytes) -> list:
        # 真实入口测试会绕过包级 reverse_image_search mock 直接调实现，
        # 把 AnimeTrace 底层供应商也 mock 掉，避免测试触碰真实外网接口。
        return []

    monkeypatch.setattr("atri_qq_bot.toolbox.reverse_image_search", _no_reverse_hits)
    monkeypatch.setattr("atri_qq_bot.toolbox.reverse_image_findings", _no_reverse_findings)
    monkeypatch.setattr("atri_qq_bot.toolbox._animetrace_hits", _no_tracemoe_like)
    monkeypatch.setattr("atri_qq_bot.toolbox.helpers._animetrace_hits", _no_tracemoe_like)
    monkeypatch.setattr("atri_qq_bot.toolbox.PROJECT_ROOT", tmp_path)
