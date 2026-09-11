from pathlib import Path
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from wechat_agent.agent import Agent, CAPABILITIES, CHANNEL_PROMPT, WORKER_PROMPT, capabilities_tool
from wechat_agent.store import Store


def test_send_file_snapshots_output_and_cannot_escape(tmp_path):
    store = Store(tmp_path / "data")
    try:
        user = store.user("local", "owner")
        message = store.ingest(user, "message", "work", {})
        workspace = store.user_root(user) / "workspace"
        (workspace / "report.txt").write_text("version1", encoding="utf-8")
        agent = Agent(store, "synthetic")
        assert agent.snapshot_file(user, message, "report.txt")["queued"]
        (workspace / "report.txt").write_text("version2", encoding="utf-8")
        delivery = store.pending_deliveries()[0]
        assert (store.root / delivery["content"]).read_text(encoding="utf-8") == "version1"
        with pytest.raises(ValueError):
            agent.snapshot_file(user, message, "../copilot/secret")
    finally:
        store.close()


def test_capability_tool_is_static_and_read_only():
    async def scenario():
        tool = capabilities_tool()
        assert tool.name == "get_capabilities"
        assert tool.parameters["additionalProperties"] is False
        assert tool.parameters["properties"] == {}
        result = await tool.handler(SimpleNamespace(arguments={}))
        assert result.result_type == "success"
        assert json.loads(result.text_result_for_llm) == CAPABILITIES
        assert "automatically queued" in CAPABILITIES["current_wechat_conversation"]["text"]
        assert any("Timers" in limitation for limitation in CAPABILITIES["not_available"])
        assert any("messaging other people" in boundary for boundary in CAPABILITIES["boundaries"])
        assert "request_confirmation before external publication, messaging other people" in WORKER_PROMPT
    asyncio.run(scenario())


def test_history_search_is_owned_bounded_and_excludes_future(tmp_path):
    store = Store(tmp_path)
    try:
        user = store.user("bot", "owner")
        other = store.user("bot", "other")
        first = store.ingest(user, "1", "100% report", {"secret": "hidden"})
        store.enqueue_text(user, first, "100% reply")
        store.ingest(other, "1", "100% private", {})
        current = store.ingest(user, "2", "find 100%", {})
        store.ingest(user, "3", "100% future", {})
        results = store.search_messages(user, current, "100%")
        assert len(results) == 2
        assert {row["role"] for row in results} == {"user", "assistant"}
        assert all(row["id"] == first and "payload" not in row for row in results)
        assert not store.search_messages(other, current, "reply")
        assert not store.search_messages(user, current, "_")
    finally:
        store.close()


@pytest.mark.parametrize("catalog", ["available", "missing", "failed"])
def test_environment_tool_reports_model_availability_without_fallback(tmp_path, catalog):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            agent = Agent(store, "synthetic")
            agent.available_models = AsyncMock(return_value=["chosen"] if catalog == "available" else [])
            if catalog == "failed":
                agent.available_models.side_effect = RuntimeError("private diagnostic")
            tool = next(tool for tool in agent.retrieval_tools(user, 1, "chosen") if tool.name == "check_environment")
            response = await tool.handler(SimpleNamespace(arguments={}))
            result = json.loads(response.text_result_for_llm)
            assert result["model"]["selected"] == "chosen"
            assert result["model"]["available"] is (None if catalog == "failed" else catalog == "available")
            assert "private diagnostic" not in response.text_result_for_llm
        finally:
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("selection", ["direct", "new", "existing", "foreign", "interrupted"])
def test_reception_delivery_and_immediate_owned_task_selection(tmp_path, selection):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            other = store.user("bot", "other")
            existing = store.create_task(user, "Existing")
            foreign = store.create_task(other, "Private")
            message_id = store.ingest(user, "1", "synthetic request", {})
            agent = Agent(store, "synthetic")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.list_models.return_value = [SimpleNamespace(id=agent.model)]
            session = AsyncMock()
            agent.client = Mock(return_value=client)
            agent.execute = AsyncMock()

            async def configure(client, owner, session_id, options):
                assert CHANNEL_PROMPT in options["system_message"]["content"]
                assert options["available_tools"] == ["custom:*"]
                decision = options["on_permission_request"](SimpleNamespace(kind="shell"), None)
                assert "Reject" in type(decision).__name__
                tools = {tool.name: tool for tool in options["tools"]}
                assert set(tools) == {"find_tasks", "start_task", "continue_task", "get_capabilities",
                                      "search_messages", "search_files", "check_environment"}

                async def respond(*args, **kwargs):
                    if selection in ("new", "interrupted"):
                        result = await tools["start_task"].handler(SimpleNamespace(arguments={"title": "Report"}))
                    elif selection in ("existing", "foreign"):
                        target = existing if selection == "existing" else foreign
                        result = await tools["continue_task"].handler(SimpleNamespace(arguments={"task_id": target["id"]}))
                    else:
                        return SimpleNamespace(data=SimpleNamespace(content="hello from this conversation"))
                    task = store.task_for_message(user, message_id)
                    if selection == "foreign":
                        assert result.result_type == "failure"
                        assert task is None
                    else:
                        assert result.result_type == "success"
                        assert task is not None
                        assert task["id"] != foreign["id"]
                        if selection == "existing":
                            assert task["id"] == existing["id"]
                        duplicate = await tools["start_task"].handler(SimpleNamespace(arguments={"title": "Duplicate"}))
                        assert duplicate.result_type == "failure"
                    if selection == "interrupted":
                        raise RuntimeError("synthetic router failure after selection")
                    return None

                session.send_and_wait.side_effect = respond
                return session

            agent.session = configure
            if selection == "interrupted":
                with pytest.raises(RuntimeError):
                    await agent.handle(user, store.pending(user)[0])
            else:
                await agent.handle(user, store.pending(user)[0])
            session.disconnect.assert_awaited_once()
            if selection in ("new", "existing"):
                agent.execute.assert_awaited_once()
                assert agent.execute.call_args.args[2]["id"] == store.task_for_message(user, message_id)["id"]
            else:
                agent.execute.assert_not_called()
            if selection == "direct":
                assert store.pending_deliveries()[0]["content"] == "[对话 · 已完成]\nhello from this conversation"
                assert store.task_for_message(user, message_id) is None
            assert user not in agent.active
        finally:
            store.close()
    asyncio.run(scenario())


def test_explicit_continuation_bypasses_router(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            task = store.create_task(user, "Report")
            message_id = store.ingest(user, "1", f"继续任务 {task['number']} 补充总结", {})
            store.bind_task(user, message_id, task["id"])
            agent = Agent(store, "synthetic")
            agent.execute = AsyncMock()
            agent.client = Mock(side_effect=AssertionError("Router must not be used"))
            await agent.handle(user, store.pending(user)[0])
            assert agent.execute.call_args.args[2]["id"] == task["id"]
            agent.client.assert_not_called()
        finally:
            store.close()
    asyncio.run(scenario())


def test_cancelled_worker_aborts_sdk_session(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            task = store.create_task(user, "Report")
            message_id = store.ingest(user, "1", "work", {})
            message = store.pending(user)[0]
            store.bind_task(user, message_id, task["id"])
            agent = Agent(store, "synthetic")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.list_models.return_value = [SimpleNamespace(id=agent.model)]
            session = AsyncMock()
            session.send_and_wait.side_effect = asyncio.CancelledError()
            agent.client = Mock(return_value=client)
            agent.session = AsyncMock(return_value=session)
            with pytest.raises(asyncio.CancelledError):
                await agent.handle(user, message)
            session.abort.assert_awaited_once()
            session.disconnect.assert_awaited_once()
            assert user not in agent.active
        finally:
            store.close()
    asyncio.run(scenario())


def test_model_choice_is_fixed_for_router_and_worker_in_one_request(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            store.set_setting("model:" + user, "model-one")
            store.ingest(user, "1", "work", {})
            agent = Agent(store, "synthetic")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.list_models.return_value = [SimpleNamespace(id="model-one")]
            session = AsyncMock()
            agent.client = Mock(return_value=client)
            agent.execute = AsyncMock()

            async def create(client, owner, session_id, options):
                assert options["model"] == "model-one"
                await options["tools"][1].handler(SimpleNamespace(arguments={"title": "Report"}))
                store.set_setting("model:" + user, "model-two")
                return session

            agent.session = create
            session.send_and_wait.return_value = None
            await agent.handle(user, store.pending(user)[0])
            assert agent.execute.call_args.kwargs["model"] == "model-one"
            assert agent.current_model(user) == "model-two"
        finally:
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("available", [True, False])
def test_worker_uses_selected_model_and_never_falls_back(tmp_path, available):
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            task = store.create_task(user, "Report")
            store.ingest(user, "1", "work", {})
            store.set_setting("model:" + user, "selected-model")
            store.set_setting("session:" + task["session_id"], "1")
            agent = Agent(store, "synthetic")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.list_models.return_value = [SimpleNamespace(id="selected-model" if available else agent.model)]
            client.resume_session.return_value.send_and_wait.return_value = None
            agent.client = Mock(return_value=client)
            if available:
                await agent.execute(user, store.pending(user)[0], task, [])
                assert client.resume_session.call_args.kwargs["model"] == "selected-model"
                options = client.resume_session.call_args.kwargs
                assert CHANNEL_PROMPT in options["system_message"]["content"]
                assert "get_capabilities" in {tool.name for tool in options["tools"]}
                assert "browser" in {tool.name for tool in options["tools"]}
                assert {"search_messages", "search_files", "check_environment", "prepare_file", "read_document", "web_search", "fetch_page"} <= {tool.name for tool in options["tools"]}
                assert options["mcp_servers"] == {}
                assert options["enable_skills"] is False
                assert options["manage_schedule_enabled"] is False
            else:
                with pytest.raises(ValueError, match="Configured model is unavailable"):
                    await agent.execute(user, store.pending(user)[0], task, [])
                client.resume_session.assert_not_called()
                client.create_session.assert_not_called()
            assert agent.current_model(user) == "selected-model"
        finally:
            store.close()
    asyncio.run(scenario())


def test_browser_closed_when_worker_fails_or_is_cancelled(tmp_path, monkeypatch):
    from wechat_agent import agent as module
    async def scenario():
        store = Store(tmp_path)
        try:
            user = store.user("bot", "owner")
            for error in (RuntimeError("failure"), asyncio.CancelledError()):
                browser = Mock(close=AsyncMock())
                monkeypatch.setattr(module, "Browser", Mock(return_value=browser))
                agent = Agent(store, "synthetic")
                agent.execute_with_browser = AsyncMock(side_effect=error)
                with pytest.raises(type(error)):
                    await agent.execute(user, {}, {}, [])
                browser.close.assert_awaited_once()
        finally:
            store.close()
    asyncio.run(scenario())


def test_known_software_execution_does_not_grant_neighbor_file_access(tmp_path, monkeypatch):
    from wechat_agent import agent as module
    async def scenario():
        store = Store(tmp_path / "data")
        try:
            user = store.user("bot", "owner")
            task = store.create_task(user, "Local program")
            store.ingest(user, "1", "run", {})
            executable = tmp_path / "tools" / "ffmpeg.exe"
            monkeypatch.setattr(module, "installed_programs", lambda: {"ffmpeg": str(executable)})
            agent = Agent(store, "synthetic")
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.list_models.return_value = [SimpleNamespace(id=agent.model)]
            agent.client = Mock(return_value=client)
            async def configure(client, owner, session_id, options):
                permission = options["on_permission_request"]
                invocation = SimpleNamespace()
                request = SimpleNamespace(kind="shell", possible_paths=[str(executable)], possible_urls=[],
                                          full_command_text=f'"{executable}" --version')
                assert "Approve" in type(await permission(request, invocation)).__name__
                request.possible_paths.append(str(executable.parent / "private.txt"))
                assert "Reject" in type(await permission(request, invocation)).__name__
                request = SimpleNamespace(kind="read", path=str(executable))
                assert "Reject" in type(await permission(request, invocation)).__name__
                request = SimpleNamespace(kind="write", file_name=str(executable))
                assert "Reject" in type(await permission(request, invocation)).__name__
                session = AsyncMock()
                session.send_and_wait.return_value = None
                return session
            agent.session = configure
            await agent.execute(user, store.pending(user)[0], task, [])
        finally:
            store.close()
    asyncio.run(scenario())