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
                assert set(tools) == {"find_tasks", "start_task", "continue_task", "get_capabilities"}

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