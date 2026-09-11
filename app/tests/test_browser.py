import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageStat

from wechat_agent.browser import Browser, executable_path_allowed, installed_programs


def test_installed_program_discovery_and_exact_executable_exception(tmp_path):
    programs = installed_programs()
    assert Path(programs["python"]).is_file()
    executable = str(tmp_path / "program.exe")
    known = {"program": executable}
    assert executable_path_allowed(executable, f'"{executable}" --version', known)
    assert executable_path_allowed(executable, f'& "{executable}" --version', known)
    assert not executable_path_allowed(tmp_path / "private.txt", f'"{executable}" --version', known)
    assert not executable_path_allowed(executable, f'cat "{executable}"', known)
    assert not executable_path_allowed(executable, 'unknown --version', known)


@pytest.mark.parametrize("approved", [True, False])
def test_external_actions_wait_for_confirmation(tmp_path, approved):
    async def scenario():
        ask = AsyncMock(return_value="YES" if approved else "NO")
        browser = Browser(tmp_path, ask)
        page = Mock(url="https://example.com/", is_closed=Mock(return_value=False))
        target = AsyncMock()
        target.count.return_value = 1
        target.get_attribute.return_value = "submit"
        page.get_by_role.return_value = target
        browser.page = page
        result = await browser.operate({"action": "click", "role": "button", "name": "Publish", "external_action": True,
                        "confirmation_summary": "Publish synthetic greeting to the test board"})
        ask.assert_awaited_once()
        assert result["performed"] is approved
        assert target.click.await_count == int(approved)
    asyncio.run(scenario())


def test_password_fields_and_ambiguous_targets_are_blocked(tmp_path):
    async def scenario():
        browser = Browser(tmp_path, AsyncMock())
        browser.page = Mock(is_closed=Mock(return_value=False))
        target = AsyncMock()
        target.count.return_value = 1
        target.get_attribute.return_value = "password"
        browser.page.get_by_label.return_value = target
        with pytest.raises(ValueError, match="Passwords"):
            await browser.operate({"action": "fill", "label": "Password", "text": "never-send"})
        target.fill.assert_not_awaited()
        target.count.return_value = 2
        with pytest.raises(ValueError, match="exactly one"):
            await browser.operate({"action": "click", "label": "Password"})
    asyncio.run(scenario())


def test_close_always_stops_runtime(tmp_path):
    async def scenario():
        browser = Browser(tmp_path, AsyncMock())
        context = AsyncMock()
        context.close.side_effect = RuntimeError("closed")
        runtime = AsyncMock()
        browser.context, browser.runtime = context, runtime
        with pytest.raises(RuntimeError):
            await browser.close()
        runtime.stop.assert_awaited_once()
        assert browser.context is None and browser.runtime is None
    asyncio.run(scenario())


def test_browser_errors_explain_missing_inputs_without_raw_logs(tmp_path):
    async def scenario():
        browser = Browser(tmp_path, AsyncMock())
        result = await browser.invoke({"action": "read"})
        assert not result["performed"] and "No selected page" in result["error"]
        browser.operate = AsyncMock(side_effect=RuntimeError("private details"))
        result = await browser.invoke({"action": "read"})
        assert "private details" not in str(result)
        browser.operate = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await browser.invoke({"action": "read"})
    asyncio.run(scenario())


def test_browser_route_blocks_private_destinations(tmp_path, monkeypatch):
    from wechat_agent import browser as module
    async def scenario():
        browser = Browser(tmp_path, AsyncMock())
        route = AsyncMock()
        route.request = Mock(url="http://127.0.0.1/")
        monkeypatch.setattr(module, "public_address", AsyncMock(side_effect=ValueError("private")))
        await browser.route(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
    asyncio.run(scenario())


@pytest.fixture
def browser_test_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/download":
                content = b"BROWSER_DOWNLOAD_VERIFIED"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="result.txt"')
            else:
                content = b'''<!doctype html><html><head><title>Browser fixture</title>
                <meta name="viewport" content="width=device-width, initial-scale=1"></head>
                <body style="margin:24px;font:18px sans-serif;background:#eef4f0;color:#202025">
                <h1>Browser fixture</h1><label>Search <input aria-label="Search"></label>
                <button onclick="document.querySelector('p').textContent='Result: '+document.querySelector('input').value">Find</button>
                <p>Ready</p><a href="/download">Download result</a>
                <a href="/second" target="_blank">Second tab</a>
                <label>Password <input type="password" aria-label="Password"></label>
                </body></html>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.skipif(not installed_programs()["chrome"], reason="Installed Chrome/Chromium required")
def test_real_installed_chrome_workflow_and_profile_isolation(tmp_path, monkeypatch, browser_test_site):
    from wechat_agent import browser as module
    async def allow_fixture(url):
        if not url.startswith(browser_test_site + "/"):
            raise ValueError("Only synthetic fixture allowed")
    monkeypatch.setattr(module, "public_address", allow_fixture)

    async def scenario():
        root = tmp_path / "first"
        root.mkdir()
        (root / "workspace").mkdir()
        browser = Browser(root, AsyncMock())
        try:
            await browser.operate({"action": "open", "url": browser_test_site + "/"})
            result = await browser.operate({"action": "read"})
            assert "Search" in result["snapshot"] and "Find" in result["snapshot"]
            await browser.operate({"action": "fill", "label": "Search", "text": "LOCAL_CHROME_OK"})
            await browser.operate({"action": "click", "role": "button", "name": "Find"})
            assert "Result: LOCAL_CHROME_OK" in (await browser.read())["snapshot"]
            for viewport in ({"width": 1280, "height": 800}, {"width": 390, "height": 844}):
                await browser.page.set_viewport_size(viewport)
                result = await browser.operate({"action": "screenshot"})
                with Image.open(root / "workspace" / result["path"]) as image:
                    assert image.size == (viewport["width"], viewport["height"])
                    assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 5
            async with browser.page.expect_download():
                await browser.operate({"action": "click", "role": "link", "name": "Download result"})
            result = await browser.operate({"action": "download", "number": 1})
            assert (root / "workspace" / result["path"]).read_bytes() == b"BROWSER_DOWNLOAD_VERIFIED"
            async with browser.context.expect_page():
                await browser.operate({"action": "click", "role": "link", "name": "Second tab"})
            tabs = await browser.operate({"action": "tabs"})
            assert len(tabs["tabs"]) >= 2
            await browser.operate({"action": "select_tab", "number": len(tabs["tabs"])})
            await browser.context.add_cookies([{"name": "synthetic_login", "value": "owner-only",
                                               "url": browser_test_site, "expires": 2000000000}])
        finally:
            await browser.close()
        assert browser.runtime is None and browser.context is None
        try:
            await browser.start()
            assert any(item["name"] == "synthetic_login" for item in await browser.context.cookies())
        finally:
            await browser.close()
        other = Browser(tmp_path / "other", AsyncMock())
        try:
            await other.start()
            assert not await other.context.cookies()
        finally:
            await other.close()
    asyncio.run(scenario())