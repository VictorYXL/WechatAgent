import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import uuid

from .credentials import CredentialError, load_github_token


async def local_test(arguments) -> dict:
    from .agent import Agent
    from .store import Store

    store = Store(arguments.data_dir)
    try:
        user = store.user("local", arguments.user)
        message_id = store.ingest(user, uuid.uuid4().hex, arguments.message, {})
        message = next(item for item in store.pending(user) if item["id"] == message_id)
        store.mark_message(message_id, "processing")
        agent = Agent(store, load_github_token(arguments.token_file), arguments.model)
        try:
            await asyncio.wait_for(agent.handle(user, message), timeout=1200)
        except BaseException:
            store.mark_message(message_id, "interrupted")
            raise
        store.mark_message(message_id, "done")
        return {"ok": True, "stage": "local_test_complete", "tasks": store.find_tasks(user),
                "outputs": [{"kind": item["kind"], "content": item["content"]}
                            for item in store.pending_deliveries() if item["message_id"] == message_id]}
    finally:
        store.close()


async def serve(arguments):
    from .agent import Agent
    from .runtime import ServiceLease
    from .service import Service
    from .store import Store
    from .weixin import WeixinClient

    with ServiceLease(arguments.data_dir, arguments.instance_id) as lease:
        credentials = json.loads((arguments.login_dir / "credentials.json").read_text(encoding="utf-8"))
        store = Store(arguments.data_dir)
        client = WeixinClient(credentials["token"], credentials["base_url"])
        try:
            agent = Agent(store, load_github_token(arguments.token_file), arguments.model)
            lease.publish()
            await Service(store, client, credentials, agent).run(stop_requested=lease.stop_requested)
        finally:
            await client.close()
            store.close()


async def diagnose(token_path: Path, state_path: Path, model: str, smoke: bool) -> dict:
    from copilot import CopilotClient
    from copilot.rpc import PermissionDecisionReject

    report = {"ok": False, "stage": "credential"}
    try:
        token = load_github_token(token_path)
        report["credential_format"] = "supported_github_user_token"
        workspace = state_path.resolve() / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        def make_client():
            return CopilotClient(
                github_token=token,
                use_logged_in_user=False,
                working_directory=str(workspace),
                base_directory=str(state_path.resolve() / "copilot"),
                mode="empty",
                log_level="error",
                enable_remote_sessions=False,
            )

        def deny_tools(request, invocation):
            return PermissionDecisionReject()

        options = {
            "model": model,
            "on_permission_request": deny_tools,
            "available_tools": [],
            "working_directory": str(workspace),
            "memory": {"enabled": False},
            "enable_session_store": False,
            "enable_config_discovery": False,
        }
        report["stage"] = "runtime_start"
        async with make_client() as client:
            report["stage"] = "authentication"
            auth = await client.get_auth_status()
            authenticated = auth.isAuthenticated
            report["authenticated"] = bool(authenticated)
            if not authenticated:
                return report
            report["stage"] = "models"
            models = await client.list_models()
            model_ids = [item.id for item in models]
            report["models"] = model_ids
            report["requested_model"] = model
            report["model_available"] = model in model_ids
            if model not in model_ids:
                return report
            if not smoke:
                report.update(ok=True, stage="ready")
                return report
            report["stage"] = "first_turn"
            session_id = "diagnostic-" + uuid.uuid4().hex
            marker = "check-" + uuid.uuid4().hex
            session = await client.create_session(session_id=session_id, **options)
            response = await session.send_and_wait(
                f"Remember this test label: {marker}. Reply with the label only.",
                timeout=120,
            )
            report["first_turn_passed"] = bool(response and marker in response.data.content)
            await session.disconnect()
        report["stage"] = "cold_resume"
        async with make_client() as client:
            session = await client.resume_session(session_id, **options)
            response = await session.send_and_wait(
                "What was the test label? Reply with the label only.", timeout=120
            )
            report["resume_passed"] = bool(response and marker in response.data.content)
            await session.disconnect()
        report.update(
            ok=bool(report["first_turn_passed"] and report["resume_passed"]),
            stage="complete",
        )
    except CredentialError as error:
        report["error"] = str(error)
    except Exception as error:
        report["error_type"] = type(error).__name__
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument("--data-dir", type=Path, default=Path("app/data/assistant"))
    stop = commands.add_parser("stop")
    stop.add_argument("--data-dir", type=Path, default=Path("app/data/assistant"))
    diagnostic = commands.add_parser("doctor")
    diagnostic.add_argument("--token-file", type=Path, required=True)
    diagnostic.add_argument("--state-dir", type=Path, default=Path("data/diagnostic"))
    diagnostic.add_argument("--model", default="gpt-6-astra")
    diagnostic.add_argument("--smoke", action="store_true")
    diagnostic.add_argument("--report-file", type=Path)
    for name in ("weixin-qr", "weixin-confirm"):
        command = commands.add_parser(name)
        command.add_argument("--login-dir", type=Path, default=Path("app/data/weixin"))
    for name in ("serve", "local-test"):
        command = commands.add_parser(name)
        command.add_argument("--token-file", type=Path, required=True)
        command.add_argument("--data-dir", type=Path, default=Path("app/data/assistant"))
        command.add_argument("--model", default="gpt-6-astra")
        if name == "serve":
            command.add_argument("--login-dir", type=Path, default=Path("app/data/weixin"))
            command.add_argument("--instance-id", help=argparse.SUPPRESS)
        else:
            command.add_argument("--user", default="test-user")
            command.add_argument("--message", required=True)
            command.add_argument("--report-file", type=Path)
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if arguments.command == "stop":
        from .runtime import request_stop

        result = request_stop(arguments.data_dir.resolve())
        print(json.dumps({"status": result}))
        raise SystemExit(1 if result == "starting_retry" else 0)
    if arguments.command == "status":
        import sqlite3

        database = (arguments.data_dir / "index.sqlite3").resolve()
        if not database.exists():
            print(json.dumps({"initialized": False}))
            return
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            result = {"initialized": True}
            for table in ("messages", "attachments", "outbox"):
                result[table] = dict(connection.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status"))
            result["deliveries_by_kind"] = [
                {"kind": row[0], "status": row[1], "count": row[2]}
                for row in connection.execute("SELECT kind,status,COUNT(*) FROM outbox GROUP BY kind,status")
            ]
            result["tasks"] = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            result["errors"] = [{"type": row[0], "count": row[1]} for row in connection.execute(
                "SELECT error,COUNT(*) FROM messages WHERE error IS NOT NULL GROUP BY error"
            )]
            print(json.dumps(result, indent=2))
        finally:
            connection.close()
        return
    if arguments.command == "serve":
        try:
            asyncio.run(serve(arguments))
        except KeyboardInterrupt:
            print("Stopped.")
        except Exception as error:
            print(json.dumps({"ok": False, "error_type": type(error).__name__}))
            raise SystemExit(1)
        return
    print("Running " + arguments.command + "; credentials and raw diagnostics are hidden.", flush=True)
    with open(os.devnull, "w") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                if arguments.command == "doctor":
                    report = asyncio.run(asyncio.wait_for(diagnose(
                        arguments.token_file.resolve(), arguments.state_dir.resolve(),
                        arguments.model, arguments.smoke,
                    ), timeout=300))
                elif arguments.command == "local-test":
                    report = asyncio.run(local_test(arguments))
                else:
                    from .weixin import create_qr, finish_login
                    function = create_qr if arguments.command == "weixin-qr" else finish_login
                    report = asyncio.run(function(arguments.login_dir.resolve()))
            except TimeoutError:
                report = {"ok": False, "stage": "deadline", "error_type": "TimeoutError"}
            except Exception as error:
                report = {"ok": False, "error_type": type(error).__name__}
    output = json.dumps(report, ensure_ascii=True, indent=2)
    if getattr(arguments, "report_file", None):
        arguments.report_file.parent.mkdir(parents=True, exist_ok=True)
        arguments.report_file.write_text(output, encoding="utf-8")
    print(output, flush=True)
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()