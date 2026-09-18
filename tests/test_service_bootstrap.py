from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from atri_qq_bot.runtime import service_bootstrap


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "bootstrap_services_enabled": True,
        "bootstrap_suwayomi_enabled": True,
        "bootstrap_suwayomi_script": "C:/nonexistent/start-suwayomi.ps1",
        "bootstrap_suwayomi_timeout_seconds": 1.0,
        "bootstrap_napcat_enabled": True,
        "manga_enabled": True,
        "manga_server_url": "http://127.0.0.1:4567",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_bootstrap_disabled_skips_everything() -> None:
    with patch.object(service_bootstrap, "_ensure_suwayomi") as suwayomi, patch.object(
        service_bootstrap, "_ensure_napcat"
    ) as napcat:
        service_bootstrap.bootstrap_plugin_services(
            _config(bootstrap_services_enabled=False)
        )
        suwayomi.assert_not_called()
        napcat.assert_not_called()


def test_bootstrap_swallows_exceptions_from_children() -> None:
    with patch.object(
        service_bootstrap, "_ensure_suwayomi", side_effect=RuntimeError("boom")
    ), patch.object(service_bootstrap, "_ensure_napcat") as napcat:
        # 引导属于尽力而为：子步骤异常不能冒泡阻塞 bot 启动。
        service_bootstrap.bootstrap_plugin_services(_config())
        napcat.assert_not_called()


def test_suwayomi_already_running_skips_script() -> None:
    with patch.object(service_bootstrap, "_http_ready", return_value=True) as ready, patch(
        "subprocess.run"
    ) as run:
        service_bootstrap._ensure_suwayomi(_config())
        ready.assert_called_once_with("http://127.0.0.1:4567")
        run.assert_not_called()


def test_suwayomi_skipped_when_manga_disabled() -> None:
    with patch.object(
        service_bootstrap, "_http_ready", return_value=False
    ) as ready, patch("subprocess.run") as run:
        service_bootstrap._ensure_suwayomi(_config(manga_enabled=False))
        ready.assert_not_called()
        run.assert_not_called()


def test_suwayomi_missing_script_only_warns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_bootstrap, "PROJECT_ROOT", tmp_path / "project")
    with patch.object(service_bootstrap, "_http_ready", return_value=False), patch(
        "subprocess.run"
    ) as run:
        # 默认脚本路径（PROJECT_ROOT.parent/Suwayomi/start-suwayomi.ps1）
        # 不存在时只告警，不执行任何命令。
        service_bootstrap._ensure_suwayomi(_config(bootstrap_suwayomi_script=""))
        run.assert_not_called()


def test_suwayomi_launches_script_and_waits_until_ready(tmp_path) -> None:
    script = tmp_path / "start-suwayomi.ps1"
    script.write_text("# fake", encoding="utf-8")
    readiness = {"count": 0}

    def _flip_ready(url: str) -> bool:
        readiness["count"] += 1
        return readiness["count"] >= 2

    with patch.object(service_bootstrap, "_http_ready", side_effect=_flip_ready), patch(
        "subprocess.run"
    ) as run:
        service_bootstrap._ensure_suwayomi(
            _config(bootstrap_suwayomi_script=str(script))
        )
        run.assert_called_once()
        command = run.call_args.args[0]
        assert "powershell.exe" == command[0]
        assert str(script) in command
        kwargs = run.call_args.kwargs
        # 禁止管道捕获：Start-Process 拉起的 java.exe 会继承管道写端，
        # 导致 communicate() 永久阻塞（Windows 句柄继承坑）。
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert "capture_output" not in kwargs
        assert readiness["count"] == 2


def test_suwayomi_timeout_does_not_raise(tmp_path) -> None:
    script = tmp_path / "start-suwayomi.ps1"
    script.write_text("# fake", encoding="utf-8")
    with patch.object(service_bootstrap, "_http_ready", return_value=False), patch(
        "subprocess.run"
    ) as run, patch("time.sleep"):
        service_bootstrap._ensure_suwayomi(
            _config(
                bootstrap_suwayomi_script=str(script),
                bootstrap_suwayomi_timeout_seconds=1.0,
            )
        )
        run.assert_called_once()


def test_napcat_already_running_skips_launcher() -> None:
    with patch.object(
        service_bootstrap,
        "napcat_webui_access",
        return_value={"configured": True, "reachable": True, "port": 6099},
    ), patch("subprocess.Popen") as popen:
        service_bootstrap._ensure_napcat(_config())
        popen.assert_not_called()


def test_napcat_unconfigured_only_warns() -> None:
    with patch.object(
        service_bootstrap,
        "napcat_webui_access",
        return_value={"configured": False, "reachable": False, "reason": "未找到 NapCat webui.json"},
    ), patch("subprocess.Popen") as popen:
        service_bootstrap._ensure_napcat(_config())
        popen.assert_not_called()


def test_napcat_not_running_launches_hidden_launcher_slice(tmp_path) -> None:
    with patch.object(
        service_bootstrap,
        "napcat_webui_access",
        return_value={"configured": True, "reachable": False, "port": 6099},
    ), patch.object(
        service_bootstrap, "_HIDDEN_LAUNCHER", tmp_path / "hidden_launcher.py"
    ), patch.object(
        service_bootstrap, "_VENV_PYTHONW", tmp_path / "pythonw.exe"
    ), patch.object(
        service_bootstrap, "_VENV_PYTHON", tmp_path / "python.exe"
    ), patch(
        "subprocess.Popen"
    ) as popen:
        (tmp_path / "hidden_launcher.py").write_text("# fake", encoding="utf-8")
        (tmp_path / "pythonw.exe").write_bytes(b"")
        service_bootstrap._ensure_napcat(_config())
        popen.assert_called_once()
        command = popen.call_args.args[0]
        assert command[1].endswith("hidden_launcher.py")
        assert command[2] == "napcat"
        assert popen.call_args.kwargs["cwd"] == str(service_bootstrap.PROJECT_ROOT)


def test_napcat_bootstrap_disabled_skips() -> None:
    with patch.object(
        service_bootstrap, "napcat_webui_access"
    ) as access, patch("subprocess.Popen") as popen:
        service_bootstrap._ensure_napcat(_config(bootstrap_napcat_enabled=False))
        access.assert_not_called()
        popen.assert_not_called()


def test_http_ready_treats_http_error_as_ready() -> None:
    import urllib.error

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch(
        "urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "u", 404, "not found", {}, None
        )
    ):
        assert service_bootstrap._http_ready("http://127.0.0.1:4567") is True

    import socket

    with patch(
        "urllib.request.urlopen", side_effect=OSError(socket.timeout())
    ):
        assert service_bootstrap._http_ready("http://127.0.0.1:4567") is False
