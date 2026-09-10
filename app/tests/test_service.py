import asyncio
from unittest.mock import AsyncMock

from wechat_agent.service import Service, file_listing_entry, parse_command
from wechat_agent.store import Store


def incoming(number, text, user="owner"):
    return {"message_type": 1, "message_id": str(number), "from_user_id": user,
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


def test_copilot_failures_request_administrator_without_leaking_details(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        agent.model = "test-model"
        agent.handle.side_effect = RuntimeError("token expired: synthetic-secret")
        agent.available_models.side_effect = RuntimeError("unauthorized: synthetic-secret")
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        try:
            await service.receive(incoming(1, "work"))
            await service.process_user(user)
            await service.receive(incoming(2, "模型"))
            replies = [item["content"] for item in store.pending_deliveries()]
            assert len(replies) == 2
            assert all("请联系管理员" in reply and "GitHub Token" in reply for reply in replies)
            assert all("synthetic-secret" not in reply for reply in replies)
        finally:
            store.close()
    asyncio.run(scenario())


def test_wechat_poll_failure_logs_administrator_hint(tmp_path, monkeypatch, capsys):
    async def scenario():
        store = Store(tmp_path)
        weixin = AsyncMock()
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, AsyncMock())

        async def rejected(cursor):
            service.shutdown.set()
            raise RuntimeError("expired: synthetic-secret")

        weixin.updates.side_effect = rejected
        monkeypatch.setattr("wechat_agent.service.asyncio.sleep", AsyncMock())
        try:
            await service.poll_loop()
            output = capsys.readouterr().out
            assert "Contact the administrator" in output
            assert "scan again" in output
            assert "synthetic-secret" not in output
        finally:
            store.close()
    asyncio.run(scenario())


def test_file_listing_format_size_and_local_time():
    from datetime import datetime, timezone

    timestamp = "2026-09-10 01:02:03"
    local_time = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    item = {"number": 5, "source": "original", "name": "report.pdf", "size_bytes": 1536, "created_at": timestamp}
    assert file_listing_entry(item) == f"5 [收到，1.5 KiB，{local_time}] report.pdf"
    for size, expected in ((0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (1048576, "1.0 MiB"), (None, "大小未知")):
        assert f"，{expected}，" in file_listing_entry({**item, "size_bytes": size})
    assert file_listing_entry({**item, "source": "outbound", "size_bytes": None, "created_at": None}) == "5 [生成，大小未知，时间未知] report.pdf"


def test_task_and_file_commands_are_owned_and_continuation_is_durable(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        other = store.user("bot", "other")
        task = store.create_task(user, "Report")
        private_task = store.create_task(other, "Private title")
        message = store.ingest(user, "upload", "", {})
        attachment = store.allocate_attachment(user, message, 0, "report.txt", "file")
        store.save_attachment(attachment, b"original")
        file_number = store.list_files(user)[0]["number"]
        try:
            for number, text in enumerate(("任务", f"任务 {task['number']}", "文件", f"文件 {file_number}",
                                           f"任务 {private_task['number']}", "文件 99999", "继续任务 99999"), 1):
                await service.receive(incoming(number, text))
            text_replies = "\n".join(item["content"] for item in store.pending_deliveries() if item["kind"] == "text")
            assert "Report" in text_replies
            assert "report.txt" in text_replies
            assert "[收到，8 B，" in text_replies
            assert "Private title" not in text_replies
            assert "未找到该任务" in text_replies
            assert len([item for item in store.pending_deliveries() if item["kind"] == "file"]) == 1
            agent.handle.assert_not_called()
            request = incoming(20, f"继续任务 {task['number']} 补充一个总结")
            await service.receive(request)
            count = len(store.pending_deliveries())
            await service.receive(request)
            assert len(store.pending_deliveries()) == count
            queued = next(item for item in store.pending(user) if item["text"])
            assert store.task_for_message(user, queued["id"])["id"] == task["id"]
            await service.process_user(user)
            assert agent.handle.await_count == 1
            assert agent.handle.call_args.args[1]["text"].endswith("补充一个总结")
        finally:
            store.close()
    asyncio.run(scenario())


def test_numbered_approval_ignores_queries_stale_numbers_and_other_users(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner", "allowed_users": ["owner", "other"]}, AsyncMock())
        user = store.user("bot", "owner")
        message = store.ingest(user, "work", "work", {})
        question = asyncio.create_task(service.ask(user, message, "Authorize action?", approval=True))
        try:
            await asyncio.sleep(0)
            number = service.confirmations[user]["number"]
            await service.receive(incoming(1, "状态"))
            await service.receive(incoming(2, "好的"))
            await service.receive(incoming(3, f"同意 {number + 1}"))
            await service.receive(incoming(4, f"同意 {number}", "other"))
            assert not service.questions[user].done()
            await service.receive(incoming(5, f"同意 {number}"))
            assert await question == "YES"
            assert store.db.execute("SELECT status FROM confirmations WHERE number=?", (number,)).fetchone()[0] == "approved"
            next_question = asyncio.create_task(service.ask(user, message, "Another action?", approval=True))
            await asyncio.sleep(0)
            next_number = service.confirmations[user]["number"]
            await service.receive(incoming(6, f"同意 {number}"))
            assert not service.questions[user].done()
            await service.receive(incoming(7, f"拒绝 {next_number}"))
            assert await next_question == "NO"
        finally:
            question.cancel()
            await asyncio.gather(question, return_exceptions=True)
            store.close()
    asyncio.run(scenario())


def test_stop_cancels_current_execution_but_retains_queue(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        started = asyncio.Event()

        async def work(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        agent.handle.side_effect = work
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        try:
            await service.receive(incoming(1, "work"))
            service.start_pending_workers()
            await asyncio.wait_for(started.wait(), 2)
            await service.receive(incoming(2, "next work"))
            await service.receive(incoming(3, "状态"))
            assert any("已运行" in item["content"] for item in store.pending_deliveries())
            await service.receive(incoming(4, "停止"))
            assert service.workers[user].cancelled()
            assert [item["text"] for item in store.pending(user)] == ["next work"]
            assert store.db.execute("SELECT status FROM messages WHERE external_id='1'").fetchone()[0] == "interrupted"
            await service.receive(incoming(5, "全部停止"))
            assert store.pending(user) == []
        finally:
            for worker in service.workers.values():
                worker.cancel()
            await asyncio.gather(*service.workers.values(), return_exceptions=True)
            store.close()
    asyncio.run(scenario())


def test_recovered_commands_do_not_reach_agent(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        try:
            store.ingest(user, "1", "帮助", {})
            await service.process_user(user)
            agent.handle.assert_not_called()
            assert store.pending(user) == []
        finally:
            store.close()
    asyncio.run(scenario())


def test_stop_all_preserves_queued_attachments_even_when_paused(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        weixin = AsyncMock()
        weixin.download.return_value = b"retained"
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        try:
            raw = incoming(1, "process this")
            raw["item_list"].append({"type": 4, "file_item": {"file_name": "material.txt"}})
            await service.receive(raw)
            await service.receive(incoming(2, "暂停队列"))
            await service.receive(incoming(3, "全部停止"))
            await service.process_user(user)
            agent.handle.assert_not_called()
            assert store.pending(user) == []
            assert store.list_files(user)[0]["name"] == "material.txt"
            assert store.db.execute("SELECT status FROM messages WHERE external_id='1'").fetchone()[0] == "cancelled"
        finally:
            store.close()
    asyncio.run(scenario())


def test_commands_match_whole_messages_only():
    assert parse_command("  状态\n") == ("status", None, "")
    assert parse_command("/cancel") == ("stop", None, "")
    assert parse_command("继续任务 12 补充一个总结") == ("continue", 12, "补充一个总结")
    assert parse_command("任务 12") == ("task", 12, "")
    assert parse_command("列举模型") == ("models", None, "")
    assert parse_command("切换模型 2") == ("switch_model", None, "2")
    assert parse_command("切换模型 gpt-6-astra") == ("switch_model", None, "gpt-6-astra")
    for text in ("分析一下学生的学习状态", "不要停止，继续生成", "任务 12 是什么意思", "文件 0", "引用：帮助"):
        assert parse_command(text) is None


def test_delete_command_syntax_is_strict():
    assert parse_command("删除 [1, 2, 1]") == ("delete_files", None, [1, 2])
    assert parse_command(" 删除 [ 1， 2 ] ") == ("delete_files", None, [1, 2])
    assert parse_command("删除 [*]") == ("delete_files", None, "*")
    for text in ("删除 []", "删除 [0]", "删除 [1, *]", "删除 [1] 然后继续", "删除 [编号1, 编号2]"):
        assert parse_command(text)[0] == "invalid_delete"
    assert parse_command("不要删除 [1]") is None


def test_delete_all_requires_owned_confirmation_and_freezes_target_set(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner", "allowed_users": ["owner", "other"]}, agent)
        user = store.user("bot", "owner")
        other = store.user("bot", "other")

        def add_file(owner, name):
            path = store.root / "outbound" / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"synthetic")
            store.enqueue(owner, None, "file", str(path.relative_to(store.root)))
            return path

        try:
            paths = [add_file(user, f"output-{index}.txt") for index in range(25)]
            foreign = add_file(other, "other.txt")
            await service.receive(incoming(1, "删除 [*]"))
            number = store.db.execute("SELECT number FROM file_deletions").fetchone()[0]
            assert len(store.file_numbers(user)) == 25
            assert all(path.exists() for path in paths)
            new_file = add_file(user, "new.txt")
            await service.receive(incoming(2, f"同意 {number}", "other"))
            assert all(path.exists() for path in paths)
            await service.receive(incoming(3, "状态"))
            assert f"等待删除确认：{number}" in store.pending_deliveries()[-1]["content"]
            await service.receive(incoming(4, f"同意 {number}"))
            assert all(not path.exists() for path in paths)
            assert foreign.exists() and new_file.exists()
            assert len(store.file_numbers(user)) == 1
            await service.receive(incoming(5, f"同意 {number}"))
            assert new_file.exists()
            assert "已失效或已处理" in store.pending_deliveries()[-1]["content"]
            agent.handle.assert_not_called()
        finally:
            store.close()
    asyncio.run(scenario())


def test_delete_confirmation_rejection_expiry_and_busy_guard(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        path = store.root / "outbound/file.txt"
        path.parent.mkdir()
        path.write_bytes(b"keep")
        store.enqueue(user, None, "file", "outbound/file.txt")
        file_number = store.file_numbers(user)[0]

        def latest():
            return store.db.execute("SELECT MAX(number) FROM file_deletions").fetchone()[0]

        try:
            await service.receive(incoming(1, f"删除 [{file_number}, 999999]"))
            assert latest() is None
            service.running[user] = {}
            await service.receive(incoming(2, "删除 [*]"))
            assert latest() is None
            service.running.clear()
            await service.receive(incoming(3, f"删除 [{file_number}]"))
            await service.receive(incoming(4, f"拒绝 {latest()}"))
            assert path.exists()
            await service.receive(incoming(5, "删除 [*]"))
            expired = latest()
            with store.db:
                store.db.execute("UPDATE file_deletions SET expires_at='2000-01-01'")
            await service.receive(incoming(6, f"同意 {expired}"))
            assert path.exists()
            await service.receive(incoming(7, "删除 [*]"))
            superseded = latest()
            await service.receive(incoming(8, "删除 [*]"))
            await service.receive(incoming(9, f"同意 {superseded}"))
            assert path.exists()
            service.running[user] = {}
            await service.receive(incoming(10, f"同意 {latest()}"))
            assert path.exists()
            service.running.clear()
            await service.receive(incoming(11, "删除 [*]"))
            store.recover()
            await service.receive(incoming(12, f"同意 {latest()}"))
            assert path.exists()
            agent.handle.assert_not_called()
        finally:
            store.close()
    asyncio.run(scenario())


def test_deleted_files_are_not_downloaded_again_or_sent_from_stale_batch(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        weixin = AsyncMock()
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, AsyncMock())
        user = store.user("bot", "owner")
        message = store.ingest(user, "upload", "", {})
        attachment = store.allocate_attachment(user, message, 0, "upload.txt", "file")
        store.save_attachment(attachment, b"keep")
        number = store.file_numbers(user)[0]
        store.set_setting("context:" + user, "synthetic")
        store.enqueue(user, None, "text", "first")
        store.resend_file(user, None, number)

        async def sent(*args):
            store.delete_files(user, [number])

        weixin.send_items.side_effect = sent
        try:
            await service.flush_outbox()
            weixin.upload.assert_not_called()
            assert weixin.send_items.await_count == 1
            await service.download_attachments(user, message, {"item_list": [{"type": 4, "file_item": {"file_name": "upload.txt"}}]})
            weixin.download.assert_not_called()
        finally:
            store.close()
    asyncio.run(scenario())


def test_model_commands_validate_persist_and_keep_numbers_stable(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        agent.model = "model-b"
        agent.available_models.return_value = ["model-b", "model-c"]
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        other = store.user("bot", "other")
        try:
            await service.receive(incoming(1, "模型"))
            assert "1 · model-b（当前使用）" in store.pending_deliveries()[-1]["content"]
            await service.receive(incoming(2, "切换模型 2"))
            assert store.setting("model:" + user) == "model-c"
            assert store.setting("model:" + other) == ""
            agent.available_models.return_value = ["model-a", "model-b", "model-c"]
            await service.receive(incoming(3, "模型"))
            assert "2 · model-c（当前使用）" in store.pending_deliveries()[-1]["content"]
            await service.receive(incoming(4, "切换模型 unavailable"))
            await service.receive(incoming(5, "切换模型 0"))
            assert store.setting("model:" + user) == "model-c"
            agent.available_models.return_value = ["model-b"]
            await service.receive(incoming(6, "切换模型 2"))
            assert store.setting("model:" + user) == "model-c"
            agent.available_models.side_effect = RuntimeError("synthetic sensitive error")
            await service.receive(incoming(7, "模型"))
            assert "synthetic sensitive error" not in store.pending_deliveries()[-1]["content"]
            assert store.setting("model:" + user) == "model-c"
            agent.available_models.side_effect = None
            await service.receive(incoming(8, "切换模型 model-b"))
            assert store.setting("model:" + user) == "model-b"
            agent.handle.assert_not_called()
        finally:
            store.close()
        reopened = Store(tmp_path)
        try:
            assert reopened.setting("model:" + user) == "model-b"
            assert reopened.setting("model_numbers:" + user) == '["model-b", "model-c", "model-a"]'
        finally:
            reopened.close()
    asyncio.run(scenario())


def test_chinese_controls_bypass_agent_and_pause_queue(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")

        async def send(number, text):
            await service.receive({"message_type": 1, "message_id": str(number), "from_user_id": "owner",
                                   "item_list": [{"type": 1, "text_item": {"text": text}}]})

        try:
            await send(1, "处理材料")
            await send(2, "暂停队列")
            await send(3, "状态")
            await send(4, "帮助")
            help_text = store.pending_deliveries()[-1]["content"]
            for command in ("任务 编号", "文件 编号", "继续任务 编号", "切换模型 编号", "同意 编号", "拒绝 编号", "删除 [*]", "删除 [编号1, 编号2]"):
                assert command in help_text
            assert "xx" not in help_text
            await send(5, "停止")
            assert len(store.pending(user)) == 1
            await service.process_user(user)
            agent.handle.assert_not_called()
            assert any("排队：1 项" in item["content"] for item in store.pending_deliveries())
            await send(6, "恢复队列")
            await service.process_user(user)
            assert agent.handle.await_count == 1
            await send(7, "新的材料")
            await send(8, "全部停止")
            assert store.pending(user) == []
        finally:
            store.close()
    asyncio.run(scenario())


def test_model_list_distinguishes_running_model_from_next_selection(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        agent = AsyncMock()
        agent.model = "model-b"
        agent.available_models.return_value = ["model-b", "model-c"]
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        try:
            service.running[user] = {"model": "model-b"}
            await service.receive(incoming(1, "模型"))
            assert "model-b（正在使用，后续请求）" in store.pending_deliveries()[-1]["content"]
            await service.receive(incoming(2, "切换模型 model-c"))
            await service.receive(incoming(3, "模型"))
            reply = store.pending_deliveries()[-1]["content"]
            assert "model-b（正在使用）" in reply
            assert "model-c（后续请求）" in reply
            agent.available_models.return_value = ["model-c"]
            await service.receive(incoming(4, "模型"))
            assert "正在使用模型：model-b" in store.pending_deliveries()[-1]["content"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_only_text_triggers_agent_and_duplicate_messages_do_not_repeat(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        weixin = AsyncMock()
        weixin.download.return_value = b"synthetic image"
        agent = AsyncMock()
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, agent)
        user = store.user("bot", "owner")
        image = {"message_type": 1, "message_id": "1", "from_user_id": "owner",
                 "item_list": [{"type": 2, "image_item": {}}]}
        try:
            await service.receive(image)
            await service.process_user(user)
            agent.handle.assert_not_called()
            assert store.pending_deliveries() == []
            question = {"message_type": 1, "message_id": "2", "from_user_id": "owner",
                        "item_list": [{"type": 1, "text_item": {"text": "Analyze that image"}}]}
            await service.receive(question)
            await service.process_user(user)
            await service.receive(question)
            await service.process_user(user)
            assert agent.handle.await_count == 1
            await service.receive({**question, "from_user_id": "stranger"})
            assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            store.close()
    asyncio.run(scenario())


def test_long_responses_are_independent_deliveries(tmp_path):
    store = Store(tmp_path)
    try:
        user = store.user("bot", "owner")
        store.enqueue(user, None, "text", "a" * 6500)
        records = store.pending_deliveries()
        assert len(records) == 3
        assert "".join(item["content"] for item in records) == "a" * 6500
        assert len({item["id"] for item in records}) == 3
    finally:
        store.close()


def test_timeout_records_uncertainty_without_rerunning_agent(tmp_path):
    import httpx

    async def scenario():
        store = Store(tmp_path)
        weixin = AsyncMock()
        weixin.send_items.side_effect = httpx.ReadTimeout("synthetic")
        agent = AsyncMock()
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, agent)
        try:
            user = store.user("bot", "owner")
            store.set_setting("context:" + user, "synthetic-context")
            store.enqueue(user, None, "text", "done")
            await service.flush_outbox()
            assert store.pending_deliveries() == []
            assert store.db.execute("SELECT status FROM outbox").fetchone()[0] == "uncertain"
            agent.handle.assert_not_called()
        finally:
            store.close()
    asyncio.run(scenario())


def test_failed_delivery_blocks_later_success_claim_for_same_message(tmp_path):
    store = Store(tmp_path)
    try:
        user = store.user("bot", "owner")
        message = store.ingest(user, "1", "make a report", {})
        file_delivery = store.enqueue(user, message, "file", "outbound/report.txt")
        summary = store.enqueue(user, message, "text", "report complete")
        assert store.delivery_ready(file_delivery)
        assert not store.delivery_ready(summary)
        store.mark_delivery(file_delivery, "failed")
        assert not store.delivery_ready(summary)
        store.mark_delivery(file_delivery, "sent")
        assert store.delivery_ready(summary)
    finally:
        store.close()


def test_slow_task_emits_status_without_exposing_reasoning(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path)
        service = Service(store, AsyncMock(), {"bot_id": "bot", "user_id": "owner"}, AsyncMock())
        calls = 0

        async def tick(seconds):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError()

        monkeypatch.setattr("wechat_agent.service.asyncio.sleep", tick)
        try:
            user = store.user("bot", "owner")
            message = store.ingest(user, "1", "work", {})
            try:
                await service.task_heartbeat(user, message)
            except asyncio.CancelledError:
                pass
            deliveries = store.pending_deliveries()
            assert len(deliveries) == 1
            assert "任务仍在运行" in deliveries[0]["content"]
        finally:
            store.close()
    asyncio.run(scenario())


def test_external_stop_request_cleans_up_service(tmp_path):
    async def scenario():
        store = Store(tmp_path)
        weixin = AsyncMock()
        polling = asyncio.Event()
        stopping = asyncio.Event()

        async def updates(*args):
            polling.set()
            await asyncio.Event().wait()

        weixin.updates.side_effect = updates
        service = Service(store, weixin, {"bot_id": "bot", "user_id": "owner"}, AsyncMock())
        runner = asyncio.create_task(service.run(stop_requested=stopping.is_set))
        try:
            await asyncio.wait_for(polling.wait(), 2)
            stopping.set()
            await asyncio.wait_for(runner, 3)
            assert service.shutdown.is_set()
            weixin.close.assert_awaited_once()
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            store.close()
    asyncio.run(scenario())