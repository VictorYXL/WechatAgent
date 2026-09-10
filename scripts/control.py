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


def choose_profile():
    print("Profile: default = your existing account and saved tasks.")
    profiles = ROOT / "app/data/profiles"
    if profiles.exists():
        print("Other saved profiles: " + ", ".join(sorted(path.name for path in profiles.iterdir() if path.is_dir())))
    print("For a new independent account, enter a new profile name (e.g. family).")
    return input("Profile name [default]: ").strip() or "default"


def start_service(data, login):
    if service_running(data):
        print("This profile is already running. No second service was started.")
        return 0
    if not (login / "credentials.json").is_file():
        print("No saved WeChat login for this profile. Run the QR login script first.")
        return 1
    if not (ROOT / "token.txt").is_file():
        print("GitHub token.txt is missing. Restore it locally; do not paste it into chat.")
        return 1
    instance_id = uuid.uuid4().hex
    command = [BACKGROUND_PYTHON, "-m", "wechat_agent.cli", "serve", "--token-file", str(ROOT / "token.txt"),
               "--data-dir", str(data), "--login-dir", str(login), "--instance-id", instance_id]
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
            print("You may close this launcher and VS Code. Send the Chinese status command in WeChat to check connectivity.")
            return 0
        try:
            result = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            continue
        print(f"Service exited during startup (exit code {result}). Check the service log and saved credentials.")
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
        print("Stop this profile's service before changing its login. Other profiles are unaffected.")
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
                            print("Login saved successfully. Run the start script for this profile.")
                            return 0
                        return 1
                    print("Login not confirmed yet. If the QR expired, enter R to generate another.")
        finally:
            for name in ("credentials.json", "pending-login.json", "login.png"):
                (staged / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Local WeChat service and QR login scripts")
    parser.add_argument("action", choices=("start", "stop", "login"))
    parser.add_argument("--profile", help="default or a separate local profile name")
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if not WINDOWS:
        os.umask(0o077)
    try:
        data, login = profile_paths(arguments.profile or choose_profile())
        if arguments.action == "start":
            return start_service(data, login)
        if arguments.action == "stop":
            return stop_service(data)
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