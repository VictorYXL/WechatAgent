import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def control(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/control.py"
    spec = importlib.util.spec_from_file_location("script_control", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def test_profiles_are_confined_and_default_preserves_existing_paths(control):
    assert control.profile_paths("default") == (control.ROOT / "app/data/assistant", control.ROOT / "app/data/weixin")
    assert control.profile_paths("Family") == control.profile_paths("family")
    for invalid in ("../outside", "a/b", "CON", "NUL", "", "a" * 41):
        with pytest.raises(ValueError):
            control.profile_paths(invalid)


def test_start_does_not_spawn_without_login_or_when_running(control, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    data, login = control.profile_paths("default")
    assert control.start_service(data, login) == 1
    with control.ServiceLease(data):
        assert control.start_service(data, login) == 0
    spawn.assert_not_called()


def test_login_stages_credentials_and_keeps_existing_allowlist(control, monkeypatch):
    data, login = control.profile_paths("default")
    login.mkdir(parents=True)
    old = {"token": "old", "bot_id": "bot", "user_id": "owner", "allowed_users": ["owner", "family"]}
    (login / "credentials.json").write_text(json.dumps(old), encoding="utf-8")
    answers = iter(["2", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(control.os, "startfile", Mock(), raising=False)

    async def qr(staged, **kwargs):
        staged.mkdir(parents=True)
        return {"ok": True, "qr_image": str(staged / "login.png")}

    async def confirm(staged):
        assert json.loads((login / "credentials.json").read_text())["token"] == "old"
        (staged / "credentials.json").write_text(json.dumps({"token": "new", "bot_id": "bot", "user_id": "owner"}))
        return {"ok": True}

    monkeypatch.setattr(control, "create_qr", qr)
    monkeypatch.setattr(control, "finish_login", confirm)
    assert asyncio.run(control.login_user(data, login)) == 0
    result = json.loads((login / "credentials.json").read_text())
    assert result["token"] == "new"
    assert result["allowed_users"] == old["allowed_users"]
    assert not (login / "scan/credentials.json").exists()


def test_cancelled_or_failed_scan_preserves_old_credentials(control, monkeypatch):
    data, login = control.profile_paths("default")
    login.mkdir(parents=True)
    original = '{"token":"old"}'
    (login / "credentials.json").write_text(original)
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    monkeypatch.setattr(control, "create_qr", AsyncMock(side_effect=OSError("synthetic")))
    with pytest.raises(OSError):
        asyncio.run(control.login_user(data, login))
    assert (login / "credentials.json").read_text() == original
    assert not control.service_running(data)


def test_different_account_requires_explicit_replacement(control, monkeypatch):
    data, login = control.profile_paths("default")
    staged = login / "scan"
    staged.mkdir(parents=True)
    old = '{"token":"old","bot_id":"bot","user_id":"owner"}'
    (login / "credentials.json").write_text(old)
    (staged / "credentials.json").write_text('{"token":"new","bot_id":"other","user_id":"other"}')
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert not control.install_login(login, staged)
    assert (login / "credentials.json").read_text() == old


def test_windows_start_detaches_and_redirects_output(control, monkeypatch):
    monkeypatch.setattr(control, "WINDOWS", True)
    data, login = control.profile_paths("default")
    login.mkdir(parents=True)
    (login / "credentials.json").write_text("{}")
    (control.ROOT / "token.txt").write_text("synthetic")
    process = Mock(pid=12345)
    process.poll.return_value = None

    def spawn(command, **kwargs):
        data.mkdir(parents=True, exist_ok=True)
        (data / "service-state.json").write_text(json.dumps({"pid": 99999, "instance": command[-1]}))
        assert command[0] == control.BACKGROUND_PYTHON
        assert command[1:4] == ["-m", "wechat_agent.cli", "serve"]
        assert command[command.index("--data-dir") + 1] == str(data)
        assert command[command.index("--login-dir") + 1] == str(login)
        assert kwargs["cwd"] == control.ROOT
        assert kwargs["creationflags"] == 8 | 512
        assert kwargs["stdin"] == control.subprocess.DEVNULL
        assert kwargs["stderr"] == control.subprocess.STDOUT
        assert not kwargs["stdout"].closed
        assert kwargs["close_fds"] is True
        assert "start_new_session" not in kwargs
        return process

    monkeypatch.setattr(control.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(control.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    assert control.start_service(data, login) == 0
    assert (data / "service.log").is_file()


def test_stop_only_signals_selected_profile(control):
    data, login = control.profile_paths("default")
    other_data, other_login = control.profile_paths("other")
    with control.ServiceLease(data) as first, control.ServiceLease(other_data) as second:
        first.publish()
        second.publish()
        assert control.stop_service(data) == 0
        assert first.stop_requested()
        assert not second.stop_requested()


def test_linux_start_detaches_and_redirects_output(control, monkeypatch):
    monkeypatch.setattr(control, "WINDOWS", False)
    data, login = control.profile_paths("default")
    login.mkdir(parents=True)
    (login / "credentials.json").write_text("{}")
    (control.ROOT / "token.txt").write_text("synthetic")
    process = Mock(pid=23456)
    process.poll.return_value = None

    def spawn(command, **kwargs):
        (data / "service-state.json").write_text(json.dumps({"pid": 99999, "instance": command[-1]}))
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == control.subprocess.DEVNULL
        assert kwargs["stderr"] == control.subprocess.STDOUT
        assert not kwargs["stdout"].closed
        assert "creationflags" not in kwargs
        return process

    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    assert control.start_service(data, login) == 0
    assert (data / "service.log").is_file()


def test_linux_login_requests_terminal_qr_without_desktop(control, monkeypatch):
    monkeypatch.setattr(control, "WINDOWS", False)
    monkeypatch.setattr("builtins.input", lambda prompt: "q")
    desktop = Mock(side_effect=AssertionError("No desktop on Linux"))
    monkeypatch.setattr(control.os, "startfile", desktop, raising=False)
    qr = AsyncMock(return_value={"ok": True, "qr_image": "synthetic.png"})
    monkeypatch.setattr(control, "create_qr", qr)
    data, login = control.profile_paths("default")
    assert asyncio.run(control.login_user(data, login)) == 1
    qr.assert_awaited_once_with(login / "scan", terminal=True)
    desktop.assert_not_called()


@pytest.mark.parametrize("filename", ["start-service.sh", "stop-service.sh", "login-wechat.sh"])
def test_shell_launcher_syntax(filename):
    shell = shutil.which("bash")
    if not shell:
        git = shutil.which("git")
        candidate = Path(git).resolve().parent.parent / "bin/bash.exe" if git else None
        if candidate and candidate.is_file():
            shell = str(candidate)
    if not shell:
        pytest.skip("A POSIX shell is not available")
    script = Path(__file__).resolve().parents[2] / filename
    assert b"\r\n" not in script.read_bytes()
    subprocess.run([shell, "-n", str(script)], check=True, capture_output=True, timeout=10)


def test_headless_qr_uses_library_terminal_renderer(tmp_path, monkeypatch):
    import qrcode
    from wechat_agent import weixin

    client = AsyncMock()
    client.qr_code.return_value = {"qrcode": "synthetic", "qrcode_img_content": "https://example.invalid/qr"}
    monkeypatch.setattr(weixin, "WeixinClient", lambda: client)
    render = Mock()
    monkeypatch.setattr(qrcode.QRCode, "print_ascii", render)
    report = asyncio.run(weixin.create_qr(tmp_path, terminal=True))
    assert Path(report["qr_image"]).is_file()
    render.assert_called_once_with(invert=True)
    client.close.assert_awaited_once()


@pytest.mark.skipif(os.name != "nt", reason="Windows detached process verification")
def test_windows_background_service_survives_launcher_exit(control):
    import ctypes
    from ctypes import wintypes

    data, login = control.profile_paths("default")
    login.mkdir(parents=True)
    (login / "credentials.json").write_text("{}")
    (control.ROOT / "token.txt").write_text("synthetic")
    worker_code = """
import ctypes
from pathlib import Path
import sys
import threading
from wechat_agent.runtime import ServiceLease
with ServiceLease(Path(sys.argv[1]), sys.argv[2]) as lease:
    print('console=' + str(bool(ctypes.windll.kernel32.GetConsoleWindow())), flush=True)
    lease.publish()
    threading.Event().wait(30)
"""
    launcher_code = """
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import control
control.ROOT = Path(sys.argv[2])
original_spawn = control.subprocess.Popen
def spawn(command, **options):
    return original_spawn([command[0], '-c', sys.argv[3], sys.argv[4], command[-1]], **options)
control.subprocess.Popen = spawn
raise SystemExit(control.start_service(*control.profile_paths('default')))
"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        result = subprocess.run([sys.executable, "-c", launcher_code, str(Path(control.__file__).parent),
                                 str(control.ROOT), worker_code, str(data)],
                                capture_output=True, text=True, timeout=25)
        state = json.loads((data / "service-state.json").read_text())
        handle = kernel.OpenProcess(0x100001, False, state["pid"])
        assert handle
        assert result.returncode == 0, result.stdout + result.stderr
        assert control.service_running(data)
        assert kernel.WaitForSingleObject(handle, 0) == 258
        assert "console=False" in (data / "service.log").read_text()
    finally:
        if handle:
            kernel.TerminateProcess(handle, 0)
            kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)