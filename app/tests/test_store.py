import pytest

from wechat_agent.store import Store, confined_path, safe_filename


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path)
    yield instance
    instance.close()


def test_messages_deduplicate_and_users_stay_separate(store):
    alice = store.user("wechat", "alice")
    bob = store.user("wechat", "bob")
    first = store.ingest(alice, "1", "hello", {})
    assert store.ingest(alice, "1", "duplicate", {}) == first
    assert store.ingest(bob, "1", "other", {}) != first
    assert len(store.pending(alice)) == 1
    task = store.create_task(alice, "A homework")
    store.summarize_task(alice, task["id"], "Algebra worksheet")
    assert store.find_tasks(alice, "Algebra")[0]["id"] == task["id"]
    assert store.find_tasks(bob) == []
    with pytest.raises(ValueError):
        store.get_task(bob, task["id"])


def test_media_is_stored_without_creating_task_and_original_is_retained(store):
    user = store.user("wechat", "alice")
    message = store.ingest(user, "media1", "", {})
    attachment = store.allocate_attachment(user, message, 0, "../../report.txt", "file")
    store.save_attachment(attachment, b"original")
    workspace = store.user_root(user) / "workspace"
    (workspace / attachment["path"]).write_bytes(b"edited")
    assert (store.user_root(user) / "originals" / attachment["path"]).read_bytes() == b"original"
    assert store.attachments(user, message)[0]["status"] == "ready"
    assert store.find_tasks(user) == []
    assert store.allocate_attachment(user, message, 0, "same.txt", "file")["id"] == attachment["id"]


def test_recovery_does_not_repeat_side_effects(store):
    user = store.user("wechat", "alice")
    message = store.ingest(user, "1", "work", {})
    store.mark_message(message, "processing")
    delivery = store.enqueue(user, message, "text", "done")
    store.mark_delivery(delivery, "sending")
    store.recover()
    assert store.pending(user) == []
    assert store.pending_deliveries() == []
    assert store.db.execute("SELECT status FROM messages").fetchone()[0] == "interrupted"


def test_attachment_snapshot_excludes_future_messages(store):
    user = store.user("wechat", "alice")
    question = store.ingest(user, "question", "start", {})
    later = store.ingest(user, "later", "", {})
    store.allocate_attachment(user, later, 0, "new.txt", "file")
    assert store.attachments(user, question) == []


def test_paths_reject_escape_and_windows_reserved_names(tmp_path):
    with pytest.raises(ValueError):
        confined_path(tmp_path, "../secret")
    assert safe_filename("C:\\secret\\report.txt") == "report.txt"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename("...") == "attachment.bin"


def test_numbered_tasks_and_files_are_stable_and_owned(tmp_path):
    store = Store(tmp_path)
    alice = store.user("bot", "alice")
    bob = store.user("bot", "bob")
    message = store.ingest(alice, "1", "work", {})
    task = store.create_task(alice, "Report")
    store.bind_task(alice, message, task["id"])
    assert store.task_for_message(alice, message)["number"] == task["number"]
    assert store.task_for_message(bob, message) is None
    assert store.task_state(alice, task["id"]) == "pending"
    attachment = store.allocate_attachment(alice, message, 0, "report.txt", "file")
    store.save_attachment(attachment, b"original")
    number = store.list_files(alice)[0]["number"]
    (store.user_root(alice) / "workspace" / attachment["path"]).write_bytes(b"edited")
    delivery = store.resend_file(alice, message, number)
    path = store.db.execute("SELECT content FROM outbox WHERE id=?", (delivery,)).fetchone()[0]
    assert (store.root / path).read_bytes() == b"original"
    with pytest.raises(ValueError):
        store.task_by_number(bob, task["number"])
    with pytest.raises(ValueError):
        store.resend_file(bob, message, number)
    store.close()
    store = Store(tmp_path)
    try:
        assert store.task_by_number(alice, task["number"])["id"] == task["id"]
        assert store.file_by_number(alice, number)["name"] == "report.txt"
        assert len(store.list_files(alice)) == 1
    finally:
        store.close()


def test_existing_database_backfills_numbers_without_reading_file_contents(tmp_path):
    import sqlite3

    store = Store(tmp_path)
    user = store.user("bot", "owner")
    task = store.create_task(user, "Existing")
    message = store.ingest(user, "1", "work", {})
    attachment = store.allocate_attachment(user, message, 0, "upload.txt", "file")
    store.save_attachment(attachment, b"old")
    store.enqueue(user, message, "file", "outbound/old/result.txt")
    store.close()
    with sqlite3.connect(tmp_path / "index.sqlite3") as database:
        database.execute("DROP TABLE task_numbers")
        database.execute("DROP TABLE files")
        database.execute("DELETE FROM settings WHERE key='file_index_initialized'")
    store = Store(tmp_path)
    try:
        assert store.task_by_number(user, 1)["id"] == task["id"]
        assert {item["source"] for item in store.list_files(user)} == {"original", "outbound"}
        store.recover()
    finally:
        store.close()


def test_file_metadata_uses_original_bytes_and_first_event_times(store):
    user = store.user("bot", "owner")
    other = store.user("bot", "other")
    message = store.ingest(user, "1", "", {})
    attachment = store.allocate_attachment(user, message, 0, "upload.txt", "file")
    store.save_attachment(attachment, b"original")
    (store.user_root(user) / "workspace" / attachment["path"]).write_bytes(b"changed working copy")
    output = store.root / "outbound/result.txt"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"result")
    delivery = store.enqueue(user, message, "file", "outbound/result.txt")
    with store.db:
        store.db.execute("UPDATE messages SET created_at='2026-09-01 01:00:00' WHERE id=?", (message,))
        store.db.execute("UPDATE outbox SET created_at='2026-09-02 02:00:00' WHERE id=?", (delivery,))
    files = store.list_files(user)
    generated, received = files
    assert received["size_bytes"] == 8
    assert received["created_at"] == "2026-09-01 01:00:00"
    assert generated["size_bytes"] == 6
    assert generated["created_at"] == "2026-09-02 02:00:00"
    store.resend_file(user, message, generated["number"])
    assert store.list_files(user) == files
    assert store.list_files(other) == []
    output.unlink()
    missing = store.list_files(user)[0]
    assert missing["size_bytes"] is None
    assert missing["created_at"] == generated["created_at"]


def test_file_deletion_removes_owned_library_files_and_cancels_copies(store):
    user = store.user("bot", "owner")
    other = store.user("bot", "other")
    message = store.ingest(user, "1", "", {})
    attachment = store.allocate_attachment(user, message, 0, "upload.txt", "file")
    store.save_attachment(attachment, b"original")
    number = store.file_numbers(user)[0]
    copy_id = store.resend_file(user, message, number)
    copy_path = store.db.execute("SELECT content FROM outbox WHERE id=?", (copy_id,)).fetchone()[0]
    with pytest.raises(ValueError):
        store.delete_files(other, [number])
    store.mark_delivery(copy_id, "sending")
    with pytest.raises(ValueError):
        store.delete_files(user, [number])
    store.mark_delivery(copy_id, "pending")
    assert store.delete_files(user, [number, number]) == {"deleted": [number], "failed": []}
    assert store.list_files(user) == []
    assert store.attachments(user, message) == []
    assert not (store.root / copy_path).exists()
    assert not (store.user_root(user) / "originals" / attachment["path"]).exists()
    assert not (store.user_root(user) / "workspace" / attachment["path"]).exists()
    assert store.db.execute("SELECT status FROM outbox WHERE id=?", (copy_id,)).fetchone()[0] == "cancelled"
    assert (store.user_root(user) / "workspace/pyproject.toml").exists()
    with pytest.raises(ValueError):
        store.resend_file(user, message, number)


def test_file_deletion_validates_entire_selection_before_removal(store):
    user = store.user("bot", "owner")
    output = store.root / "outbound/report.txt"
    output.parent.mkdir()
    output.write_bytes(b"report")
    store.enqueue(user, None, "file", "outbound/report.txt")
    number = store.file_numbers(user)[0]
    with pytest.raises(ValueError):
        store.delete_files(user, [number, 999999])
    assert output.exists()
    assert store.delete_files(user, [number])["deleted"] == [number]
    assert not output.exists()
    store.close()
    reopened = Store(store.root)
    try:
        assert reopened.list_files(user) == []
    finally:
        reopened.close()


def test_file_deletion_reports_partial_failure_and_allows_retry(store, monkeypatch):
    from pathlib import Path

    user = store.user("bot", "owner")
    message = store.ingest(user, "1", "", {})
    attachment = store.allocate_attachment(user, message, 0, "upload.txt", "file")
    store.save_attachment(attachment, b"original")
    number = store.file_numbers(user)[0]
    working = store.user_root(user) / "workspace" / attachment["path"]
    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == working:
            raise PermissionError("synthetic locked file")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", blocked)
        assert store.delete_files(user, [number]) == {"deleted": [], "failed": [number]}
    assert store.file_numbers(user) == [number]
    assert working.exists()
    assert store.delete_files(user, [number]) == {"deleted": [number], "failed": []}


def test_file_deletion_preflights_all_paths_before_removal(store):
    user = store.user("bot", "owner")
    output = store.root / "outbound/report.txt"
    output.parent.mkdir()
    output.write_bytes(b"keep")
    private = store.root / "private.txt"
    private.write_bytes(b"keep private")
    store.enqueue(user, None, "file", "outbound/report.txt")
    store.enqueue(user, None, "file", "private.txt")
    with pytest.raises(ValueError):
        store.delete_files(user, store.file_numbers(user))
    assert output.read_bytes() == b"keep"
    assert private.read_bytes() == b"keep private"