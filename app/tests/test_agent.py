from pathlib import Path
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from wechat_agent.agent import Agent
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
            else:
                with pytest.raises(ValueError, match="Configured model is unavailable"):
                    await agent.execute(user, store.pending(user)[0], task, [])
                client.resume_session.assert_not_called()
                client.create_session.assert_not_called()
            assert agent.current_model(user) == "selected-model"
        finally:
            store.close()
    asyncio.run(scenario())