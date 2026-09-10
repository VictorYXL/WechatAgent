import asyncio
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

from wechat_agent.runtime import request_stop, service_running
from wechat_agent.server import Server
from wechat_agent.login_web import LoginWeb


def test_empty_server_stays_running_until_server_stop(tmp_path):
    async def scenario():
        server = Server(tmp_path)
        task = asyncio.create_task(server.run())
        try:
            await asyncio.sleep(0)
            assert not task.done()
            assert service_running(server.directory)
            assert request_stop(server.directory) == "stop_requested"
            await asyncio.wait_for(task, 2)
            assert not service_running(server.directory)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_new_login_is_discovered_without_server_restart(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def worker(arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        mock = AsyncMock(side_effect=worker)
        monkeypatch.setattr("wechat_agent.server.serve", mock)
        await server.reconcile()
        mock.assert_not_called()
        login = tmp_path / "app/data/weixin"
        login.mkdir(parents=True)
        (login / "credentials.json").write_text("{}")
        await server.reconcile()
        await asyncio.wait_for(started.wait(), 1)
        await server.reconcile()
        assert mock.call_count == 1
        await server.close()
        assert stopped.is_set()
        assert not server.tasks
    asyncio.run(scenario())


def test_detached_manager_survives_launcher_and_stops_cleanly(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts"
    launcher = """
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import control
control.ROOT = Path(sys.argv[2])
raise SystemExit(control.control_server('start'))
"""
    directory = tmp_path / "app/data/server"
    try:
        result = subprocess.run([sys.executable, "-c", launcher, str(script), str(tmp_path)],
                                capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stdout
        assert service_running(directory)
        assert (directory / "web.json").exists()
        assert request_stop(directory) == "stop_requested"
    finally:
        request_stop(directory)

        async def stopped():
            while service_running(directory):
                await asyncio.sleep(0.1)

        asyncio.run(asyncio.wait_for(stopped(), 15))
    assert not (directory / "web.json").exists()


def test_account_identity_renewal_preserves_allowlist_and_isolation(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path)
        monkeypatch.setattr(server, "reconcile", AsyncMock())
        first = {"bot_id": "bot-one", "user_id": "owner", "token": "old"}
        await server.accept_login(first)
        default = tmp_path / "app/data/weixin/credentials.json"
        saved = {**first, "allowed_users": ["owner", "family"]}
        default.write_text(json.dumps(saved))
        await server.accept_login({**first, "token": "new"})
        assert json.loads(default.read_text())["allowed_users"] == ["owner", "family"]
        await server.accept_login({"bot_id": "bot-two", "user_id": "other", "token": "other"})
        assert json.loads(default.read_text())["token"] == "new"
        assert len(list((tmp_path / "app/data/profiles").glob("*/weixin/credentials.json"))) == 1
        assert server.reconcile.await_count == 3
    asyncio.run(scenario())


def test_web_requires_capability_and_rejects_cross_origin(tmp_path):
    async def scenario():
        web = LoginWeb(Server(tmp_path))
        await web.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{web.http.server_port}") as client:
                page = await client.get("/")
                assert page.status_code == 200
                assert web.key not in page.text
                assert "no-store" in page.headers["cache-control"]
                assert (await client.get("/api/status")).status_code == 403
                headers = {"Authorization": "Bearer " + web.key}
                assert (await client.get("/api/status", headers=headers)).json()["status"] == "running"
                assert (await client.post("/api/qr", headers={**headers, "Origin": "https://evil.test"}, json={})).status_code == 403
                assert (await client.get("/api/status", headers={**headers, "Host": "evil.test"})).status_code == 403
                assert (await client.post("/api/qr", headers=headers, content=b"x" * 4097)).status_code == 400
        finally:
            await web.close()
        assert not web.state_path.exists()
    asyncio.run(scenario())


def test_qr_confirm_cancel_expiry_and_failure_keep_secrets_local(tmp_path, monkeypatch):
    async def scenario():
        server = Server(tmp_path)
        web = LoginWeb(server)
        monkeypatch.setattr(server, "accept_login", AsyncMock())

        async def create(directory):
            directory.mkdir(parents=True)
            (directory / "login.png").write_bytes(b"synthetic-png")

        async def finish(directory):
            (directory / "credentials.json").write_text(json.dumps({"token": "synthetic-secret"}))
            return {"ok": True}

        monkeypatch.setattr("wechat_agent.login_web.create_qr", create)
        monkeypatch.setattr("wechat_agent.login_web.finish_login", finish)
        first = await web.handle("POST", "/api/qr", {})
        assert first["image"].startswith("data:image/png;base64,")
        assert await web.handle("POST", "/api/cancel", first) == {"status": "cancelled"}
        server.accept_login.assert_not_called()
        second = await web.handle("POST", "/api/qr", {})
        web.sessions[second["session"]]["expires"] = 0
        assert await web.handle("POST", "/api/confirm", second) == {"status": "expired"}
        third = await web.handle("POST", "/api/qr", {})
        assert await web.handle("POST", "/api/confirm", third) == {"status": "connected"}
        server.accept_login.assert_awaited_once_with({"token": "synthetic-secret"})
        assert not web.sessions
        assert not list(web.scan_root.iterdir())
        fourth = await web.handle("POST", "/api/qr", {})
        server.accept_login.side_effect = RuntimeError("synthetic")
        with pytest.raises(RuntimeError):
            await web.handle("POST", "/api/confirm", fourth)
        assert not web.sessions
        assert not list(web.scan_root.iterdir())
    asyncio.run(scenario())