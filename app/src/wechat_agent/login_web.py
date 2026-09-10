import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import threading
import time

from .weixin import create_qr, finish_login, save_private_json


class LoginWeb:
    def __init__(self, server):
        self.server = server
        self.key = secrets.token_urlsafe(32)
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.http = None
        self.thread = None
        self.state_path = server.directory / "web.json"
        self.scan_root = server.directory / "web-scans"

    async def start(self):
        loop = asyncio.get_running_loop()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, content, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(content)

            def dispatch(self):
                self.connection.settimeout(60)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 65536:
                        raise ValueError()
                    payload = self.rfile.read(length)
                    if length > 4096:
                        raise ValueError()
                except (ValueError, TimeoutError):
                    self.respond(400, b'{"error":"Invalid request size"}')
                    return
                expected = f"127.0.0.1:{owner.http.server_port}"
                if self.headers.get("Host") != expected or self.headers.get("Origin", "http://" + expected) != "http://" + expected:
                    self.respond(403, b'{"error":"Local access only"}')
                    return
                if self.command == "GET" and self.path == "/":
                    self.respond(200, Path(__file__).with_name("login.html").read_bytes(), "text/html; charset=utf-8")
                    return
                if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.key):
                    self.respond(403, b'{"error":"Open the login launcher to authorize this window."}')
                    return
                try:
                    body = json.loads(payload) if payload else {}
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    self.respond(400, b'{"error":"Invalid request"}')
                    return
                future = asyncio.run_coroutine_threadsafe(owner.handle(self.command, self.path, body), loop)
                try:
                    result = future.result(timeout=55)
                    self.respond(200, json.dumps(result).encode())
                except FutureTimeout:
                    future.cancel()
                    self.respond(504, b'{"error":"Login timed out. Contact the administrator or try a new QR code."}')
                except Exception:
                    self.respond(503, b'{"error":"Login unavailable. Contact the administrator."}')

            do_GET = dispatch
            do_POST = dispatch

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        shutil.rmtree(self.scan_root, ignore_errors=True)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        save_private_json(self.state_path, {"port": self.http.server_port, "key": self.key})

    def expire_sessions(self):
        for identifier, session in list(self.sessions.items()):
            if time.monotonic() >= session["expires"]:
                shutil.rmtree(session["directory"], ignore_errors=True)
                self.sessions.pop(identifier)

    async def handle(self, method, path, body):
        async with self.lock:
            self.expire_sessions()
            if self.server.closing:
                return {"error": "Server is stopping."}
            if method == "GET" and path == "/api/status":
                return {"status": "running", "accounts": sum(not task.done() for task in self.server.tasks.values())}
            if method != "POST":
                return {"error": "Unknown request."}
            if path == "/api/qr":
                old = self.sessions.pop(body.get("session", ""), None)
                if old:
                    shutil.rmtree(old["directory"], ignore_errors=True)
                if len(self.sessions) >= 8:
                    return {"error": "Too many login windows. Try again later."}
                identifier = secrets.token_hex(16)
                directory = self.scan_root / identifier
                try:
                    await create_qr(directory)
                    image = base64.b64encode((directory / "login.png").read_bytes()).decode()
                except BaseException:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
                self.sessions[identifier] = {"directory": directory, "expires": time.monotonic() + 600}
                return {"session": identifier, "image": "data:image/png;base64," + image, "status": "scan_required"}
            if path in ("/api/confirm", "/api/cancel"):
                identifier = body.get("session", "")
                session = self.sessions.get(identifier)
                if not session:
                    return {"status": "expired"}
                directory = session["directory"]
                if path == "/api/cancel":
                    self.sessions.pop(identifier)
                    shutil.rmtree(directory, ignore_errors=True)
                    return {"status": "cancelled"}
                result = await finish_login(directory)
                if result["ok"]:
                    try:
                        credentials = json.loads((directory / "credentials.json").read_text(encoding="utf-8"))
                        await self.server.accept_login(credentials)
                    finally:
                        self.sessions.pop(identifier)
                        shutil.rmtree(directory, ignore_errors=True)
                    return {"status": "connected"}
                return {"status": result["status"]}
            return {"error": "Unknown request."}

    async def close(self):
        if self.http:
            await asyncio.to_thread(self.http.shutdown)
            self.http.server_close()
            self.state_path.unlink(missing_ok=True)
            shutil.rmtree(self.scan_root, ignore_errors=True)
            self.sessions.clear()