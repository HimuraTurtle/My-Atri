"""全局测试隔离。

未显式传入 memory_path 等路径的 BotConfig 会把默认数据路径解析到真实生产
目录：历史上多次 pytest 运行把 private:10001、group:20001 等测试会话写进
data/memory/users.json，并连带新建了 data/memory/living_memory.sqlite3。
此 autouse fixture 把 schema 里的默认路径根目录重定向到各测试自己的
tmp_path，从根源切断污染；显式传入路径的测试不受影响。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_default_data_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("atri_qq_bot.config.schema._project_root", lambda: tmp_path)
