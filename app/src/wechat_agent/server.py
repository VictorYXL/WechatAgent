import asyncio
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import re

from .cli import serve
from .runtime import ServiceLease, request_stop, service_running
from .weixin import save_private_json


def account_paths(root: Path):
    yield "default", root / "app/data/assistant", root / "app/data/weixin"
    directory = root / "app/data/profiles"
    if directory.exists():
        for path in sorted(directory.iterdir()):
            if (path.is_dir() and not path.is_symlink()
                    and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", path.name)
                    and path.name != "default" and path.resolve().parent == directory.resolve()):
                yield path.name, path / "assistant", path / "weixin"


class Server:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / "app/data/server"
        self.tasks = {}
        self.retry_after = {}
        self.closing = False
        self.account_lock = asyncio.Lock()

    async def reconcile(self):
        async with self.account_lock:
            if self.closing:
                return
            for name, data, login in account_paths(self.root):
                task = self.tasks.get(name)
                if task and task.done():
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as error:
                        print("Account stopped. Contact the administrator. Error type:", type(error).__name__, flush=True)
                    self.tasks.pop(name)
                    self.retry_after[name] = asyncio.get_running_loop().time() + 60
                if name in self.tasks or asyncio.get_running_loop().time() < self.retry_after.get(name, 0):
                    continue
                if not (login / "credentials.json").is_file() or service_running(data):
                    continue
                arguments = Namespace(data_dir=data, login_dir=login, token_file=self.root / "token.txt",
                                      model="gpt-6-astra", instance_id=None)
                self.tasks[name] = asyncio.create_task(serve(arguments))

    async def stop_account(self, name, data):
        task = self.tasks.pop(name, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for attempt in range(100):
            result = request_stop(data)
            if result == "not_running":
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Account is still stopping. Contact the administrator.")

    async def close(self):
        self.closing = True
        async with self.account_lock:
            results = await asyncio.gather(*(self.stop_account(name, data)
                                            for name, data, login in account_paths(self.root)),
                                           return_exceptions=True)
            if any(isinstance(result, Exception) for result in results):
                print("Some accounts did not stop in time. Contact the administrator.", flush=True)

    async def accept_login(self, credentials):
        async with self.account_lock:
            if self.closing:
                raise RuntimeError("Server is stopping")
            selected = None
            for name, data, login in account_paths(self.root):
                saved = login / "credentials.json"
                if not saved.exists():
                    continue
                previous = json.loads(saved.read_text(encoding="utf-8"))
                if all(previous.get(key) == credentials[key] for key in ("bot_id", "user_id")):
                    selected = name, data, login
                    if "allowed_users" in previous:
                        credentials = {**credentials, "allowed_users": previous["allowed_users"]}
                    break
            if selected is None:
                if not (self.root / "app/data/weixin/credentials.json").exists():
                    selected = "default", self.root / "app/data/assistant", self.root / "app/data/weixin"
                else:
                    identity = json.dumps([credentials["bot_id"], credentials["user_id"]])
                    name = "account-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                    directory = self.root / "app/data/profiles" / name
                    selected = name, directory / "assistant", directory / "weixin"
                    if (selected[2] / "credentials.json").exists():
                        raise RuntimeError("Account identity conflict")
            name, data, login = selected
            await self.stop_account(name, data)
            save_private_json(login / "credentials.json", credentials)
            self.retry_after.pop(name, None)
        await self.reconcile()

    async def run(self, instance_id=None, ready=None, finished=None):
        with ServiceLease(self.directory, instance_id) as lease:
            try:
                if ready:
                    await ready()
                lease.publish()
                print("Server running. Waiting for WeChat accounts.", flush=True)
                while not lease.stop_requested():
                    await self.reconcile()
                    await asyncio.sleep(0.5)
            finally:
                try:
                    await self.close()
                finally:
                    if finished:
                        await finished()


async def run_server(root, instance_id=None):
    from .login_web import LoginWeb

    server = Server(root)
    web = LoginWeb(server)
    await server.run(instance_id, ready=web.start, finished=web.close)


if __name__ == "__main__":
    import argparse
    import logging
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--instance-id")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if os.name != "nt":
        os.umask(0o077)
    asyncio.run(run_server(arguments.root, arguments.instance_id))