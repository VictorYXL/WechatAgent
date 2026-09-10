import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import webbrowser

from wechat_agent.runtime import ServiceLease, request_stop, service_running
from wechat_agent.weixin import create_qr, finish_login, save_private_json


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"
BACKGROUND_PYTHON = str(Path(sys.executable).with_name("pythonw.exe")) if WINDOWS else sys.executable


def profile_paths(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        raise ValueError("Use a profile name with letters, digits, hyphens or underscores (max 40).")
    if name.lower() == "default":
        return ROOT / "app/data/assistant", ROOT / "app/data/weixin"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10))}:
        raise ValueError("This profile name is reserved by Windows.")
    directory = ROOT / "app/data/profiles" / name.lower()
    return directory / "assistant", directory / "weixin"


def server_profiles():
    profiles = [("default", *profile_paths("default"))]
    directory = ROOT / "app/data/profiles"
    if directory.exists():
        for path in sorted(directory.iterdir()):
            if not path.is_dir():
                continue
            try:
                profile_paths(path.name)
            except ValueError:
                continue
            if path.name.lower() == "default" or path.resolve().parent != directory.resolve():
                continue
            profiles.append((path.name, path / "assistant", path / "weixin"))
    return profiles


def control_legacy_accounts(action):
    with ServiceLease(ROOT / "app/data/server-control"):
        profiles = server_profiles()
        if action == "start":
            profiles = [(name, data, login) for name, data, login in profiles
                        if (login / "credentials.json").is_file()]
        if not profiles:
            print("No saved WeChat accounts. Run login-wechat first, then start the server.")
            return 1
        failures = 0
        print(f"Server {action}: applying to all {len(profiles)} account profile(s).")
        for name, data, login in profiles:
            print("Account profile: " + name)
            try:
                result = start_service(data, login) if action == "start" else stop_service(data)
            except Exception as error:
                print("Account operation failed. Error type: " + type(error).__name__)
                result = 1
            failures += bool(result)
        print(f"Server {action} requests completed. Failed accounts: {failures}.")
        return 1 if failures else 0


def control_server(action):
    directory = ROOT / "app/data/server"
    with ServiceLease(ROOT / "app/data/server-launcher"):
        if action == "stop":
            result = request_stop(directory)
            if result == "not_running":
                return control_legacy_accounts("stop")
            if result == "starting_retry":
                print("Server is initializing. Please retry stop shortly.")
                return 1
            print("Server stop requested. All account connections will close; saved data is retained.")
            return 0
        if service_running(directory):
            print("Server is already running. Run login-wechat to open the sign-in window.")
            return 0
        identifier = uuid.uuid4().hex
        command = [BACKGROUND_PYTHON, "-m", "wechat_agent.server", "--root", str(ROOT), "--instance-id", identifier]
        return start_background(directory, command, identifier)


def open_login():
    directory = ROOT / "app/data/server"
    if not service_running(directory):
        print("Server is not running. Run start-service first, then login-wechat.")
        return 1
    state = json.loads((directory / "web.json").read_text(encoding="utf-8"))
    port = state["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", state["key"]):
        raise ValueError("Invalid local web state")
    url = f"http://127.0.0.1:{port}/"
    if webbrowser.open(url + "#key=" + state["key"], new=1):
        print("WeChat sign-in opened in your browser. Server: " + url)
        return 0
    print("No browser is available. Use login-wechat --terminal, or an authenticated SSH tunnel.")
    return 1


def start_service(data, login):
    if service_running(data):
        print("This profile is already running. No second service was started.")
        return 0
    if not (login / "credentials.json").is_file():
        print("No saved WeChat login for this profile. Run the QR login script first.")
        return 1
    if not (ROOT / "token.txt").is_file():
        print("GitHub token.txt is missing. Contact the administrator to restore it locally; do not paste it into chat.")
        return 1
    instance_id = uuid.uuid4().hex
    command = [BACKGROUND_PYTHON, "-m", "wechat_agent.cli", "serve", "--token-file", str(ROOT / "token.txt"),
               "--data-dir", str(data), "--login-dir", str(login), "--instance-id", instance_id]
    return start_background(data, command, instance_id)


def start_background(data, command, instance_id):
    data.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(data / "service.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, close_fds=True, **options)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            state = json.loads((data / "service-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("instance") == instance_id and process.poll() is None:
            print("Service started in the background. Log: " + str(data / "service.log"))
            if not WINDOWS:
                print("You may disconnect SSH unless your server policy terminates user processes on logout.")
            print("You may close this launcher and VS Code. Run login-wechat to sign in; no second start is needed.")
            return 0
        try:
            result = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            continue
        notice = f"Service exited during startup (exit code {result}). "
        notice += "Contact the administrator to check the service log, WeChat login and GitHub Token validity."
        print(notice)
        return 1
    print("Startup is not confirmed yet. Check the service log; do not repeatedly launch it.")
    return 1


def stop_service(data):
    result = request_stop(data)
    if result == "not_running":
        print("This profile is not running. Saved files and login have not been deleted.")
        return 0
    if result == "starting_retry":
        print("The service is still initializing. Please run this script again shortly.")
        return 1
    print("Stop requested. Active work will be interrupted; completed actions will not be undone.")
    print("Cleanup may take a moment. The service process exits when cleanup completes.")
    print("Detached task subprocesses are not guaranteed to stop. Saved files and login are retained.")
    return 0


def install_login(login, staged):
    destination = login / "credentials.json"
    replacement = json.loads((staged / "credentials.json").read_text(encoding="utf-8"))
    previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else None
    same_account = previous and all(previous.get(key) == replacement.get(key) for key in ("bot_id", "user_id"))
    if previous and not same_account:
        print("The scanned account/bot differs from this profile's saved login.")
        print("Old task data will be retained, but will not be visible to a different account.")
        print("A separate profile is recommended for a new user.")
        if input("Type REPLACE to replace this profile's login, or Enter to cancel: ").strip() != "REPLACE":
            print("Cancelled. The previous login is unchanged.")
            return False
    if same_account and "allowed_users" in previous:
        replacement["allowed_users"] = previous["allowed_users"]
    save_private_json(destination, replacement)
    return True


async def login_user(data, login):
    if service_running(data):
        print("Run stop-service before changing an active account's login; it stops all server accounts.")
        return 1
    with ServiceLease(data):
        if (login / "credentials.json").exists():
            print("A saved login exists: 1 = reuse it, 2 = scan again (renew/change account).")
            choice = input("Choice [1]: ").strip() or "1"
            if choice == "1":
                print("Saved login retained. Run the start script. Its validity has not been checked here.")
                return 0
            if choice != "2":
                print("Cancelled. Saved login unchanged.")
                return 1
        staged = login / "scan"
        try:
            while True:
                report = await create_qr(staged, terminal=not WINDOWS)
                image = Path(report["qr_image"])
                print("Scan the QR code in WeChat and confirm on your phone.")
                if WINDOWS:
                    try:
                        os.startfile(str(image))
                    except OSError:
                        print("Open the QR image manually: " + str(image))
                else:
                    print("Use a wide terminal for the QR code. Alternatively, retrieve this private image over SSH/SFTP:")
                    print(str(image))
                while True:
                    choice = input("After phone confirmation press Enter; R = new QR; Q = cancel: ").strip().lower()
                    if choice == "q":
                        print("Cancelled. Saved login unchanged.")
                        return 1
                    if choice == "r":
                        break
                    if choice:
                        continue
                    report = await finish_login(staged)
                    if report["ok"]:
                        if install_login(login, staged):
                            print("Login saved successfully. Run start-service to start all saved server accounts.")
                            return 0
                        return 1
                    print("Login not confirmed yet. If the QR expired, enter R to generate another.")
        finally:
            for name in ("credentials.json", "pending-login.json", "login.png"):
                (staged / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Local WeChat service and QR login scripts")
    parser.add_argument("action", choices=("start", "stop", "login"))
    parser.add_argument("--profile", help="terminal login only: default or a separate local account profile")
    parser.add_argument("--terminal", action="store_true", help="use the legacy terminal QR login instead of a browser")
    arguments = parser.parse_args()
    if arguments.action != "login" and (arguments.profile is not None or arguments.terminal):
        parser.error("--profile and --terminal are only supported for login")
    if arguments.profile is not None and not arguments.terminal:
        parser.error("--profile requires --terminal; browser login identifies the account automatically")
    logging.disable(logging.CRITICAL)
    if not WINDOWS:
        os.umask(0o077)
    try:
        if arguments.action in ("start", "stop"):
            return control_server(arguments.action)
        if not arguments.terminal:
            return open_login()
        data, login = profile_paths(arguments.profile or "default")
        return asyncio.run(login_user(data, login))
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.")
        return 1
    except Exception as error:
        print("Operation failed. Error type: " + type(error).__name__)
        print("Check network access, permissions and the selected profile. No credentials are displayed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())