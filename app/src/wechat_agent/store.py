import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid


def confined_path(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("Path is outside the user workspace")
    return candidate


def safe_filename(name: str) -> str:
    basename = re.split(r"[/\\]", name)[-1]
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", basename).strip(" .")
    if not cleaned:
        return "attachment.bin"
    if cleaned.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        cleaned = "_" + cleaned
    return cleaned[:160]


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, external_id TEXT NOT NULL,
                UNIQUE(account, external_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                external_id TEXT NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                message_id INTEGER NOT NULL REFERENCES messages(id), item_index INTEGER NOT NULL,
                name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                UNIQUE(message_id, item_index)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                message_id INTEGER REFERENCES messages(id), kind TEXT NOT NULL,
                content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_numbers (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS message_tasks (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id),
                task_id TEXT NOT NULL REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS files (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                source TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
                UNIQUE(user_id, source, path)
            );
            CREATE TABLE IF NOT EXISTS confirmations (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                message_id INTEGER NOT NULL REFERENCES messages(id),
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS deleted_files (
                number INTEGER PRIMARY KEY REFERENCES files(number)
            );
            CREATE TABLE IF NOT EXISTS file_copies (
                number INTEGER NOT NULL REFERENCES files(number), path TEXT NOT NULL,
                PRIMARY KEY(number, path)
            );
            CREATE TABLE IF NOT EXISTS file_deletions (
                number INTEGER PRIMARY KEY REFERENCES confirmations(number),
                targets TEXT NOT NULL, expires_at TEXT NOT NULL
            );
        """)
        with self.db:
            self.db.execute("INSERT INTO task_numbers(task_id) SELECT id FROM tasks WHERE NOT EXISTS "
                            "(SELECT 1 FROM task_numbers WHERE task_id=tasks.id) ORDER BY rowid")
            if not self.setting("file_index_initialized"):
                for row in self.db.execute("SELECT user_id,path,name FROM attachments WHERE status='ready'").fetchall():
                    self.register_file(row["user_id"], "original", row["path"], row["name"])
                for row in self.db.execute("SELECT user_id,content FROM outbox WHERE kind='file' ORDER BY rowid").fetchall():
                    self.register_file(row["user_id"], "outbound", row["content"], Path(row["content"]).name)
                self.db.execute("INSERT INTO settings VALUES ('file_index_initialized','1')")

    def close(self):
        self.db.close()

    def user(self, account: str, external_id: str) -> str:
        user_id = hashlib.sha256(json.dumps([account, external_id]).encode()).hexdigest()[:32]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users VALUES (?, ?, ?)",
                            (user_id, account, external_id))
        for directory in ("workspace", "originals", "copilot"):
            (self.root / "users" / user_id / directory).mkdir(parents=True, exist_ok=True)
        project = self.root / "users" / user_id / "workspace" / "pyproject.toml"
        if not project.exists():
            project.write_text('[project]\nname = "user-workspace"\nversion = "0.1.0"\n'
                               'requires-python = ">=3.11"\ndependencies = []\n', encoding="utf-8")
        return user_id

    def user_root(self, user_id: str) -> Path:
        if not self.db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise ValueError("Unknown user")
        return self.root / "users" / user_id

    def setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def ingest(self, user_id: str, external_id: str, text: str, payload: dict) -> int:
        if not external_id:
            raise ValueError("Missing message ID")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO messages(user_id, external_id, text, payload) VALUES (?,?,?,?)",
                (user_id, external_id, text, json.dumps(payload, ensure_ascii=False)),
            )
        return self.db.execute("SELECT id FROM messages WHERE user_id=? AND external_id=?",
                               (user_id, external_id)).fetchone()[0]

    def pending(self, user_id: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM messages WHERE user_id=? AND status IN ('pending','cancelled_media') ORDER BY id", (user_id,)
        )]

    def mark_message(self, message_id: int, status: str, error: str | None = None):
        with self.db:
            self.db.execute("UPDATE messages SET status=?,error=? WHERE id=?",
                            (status, error, message_id))

    def recover(self):
        with self.db:
            self.db.execute("UPDATE messages SET status='interrupted' WHERE status='processing'")
            self.db.execute("UPDATE messages SET status='pending' WHERE status='control'")
            self.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE confirmations SET status='expired' WHERE status='pending'")

    def allocate_attachment(self, user_id: str, message_id: int, item_index: int,
                            name: str, kind: str) -> dict:
        owner = self.db.execute("SELECT user_id FROM messages WHERE id=?", (message_id,)).fetchone()
        if not owner or owner[0] != user_id:
            raise ValueError("Message does not belong to user")
        attachment_id = uuid.uuid4().hex
        filename = safe_filename(name)
        relative = f"inbox/{attachment_id}/{filename}"
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO attachments(id,user_id,message_id,item_index,name,kind,path) "
                "VALUES(?,?,?,?,?,?,?)",
                (attachment_id, user_id, message_id, item_index, filename, kind, relative),
            )
        return dict(self.db.execute("SELECT * FROM attachments WHERE message_id=? AND item_index=?",
                                    (message_id, item_index)).fetchone())

    def save_attachment(self, attachment: dict, content: bytes):
        root = self.user_root(attachment["user_id"])
        original = confined_path(root / "originals", attachment["path"])
        working = confined_path(root / "workspace", attachment["path"])
        original.parent.mkdir(parents=True, exist_ok=True)
        working.parent.mkdir(parents=True, exist_ok=True)
        temporary = original.with_name(original.name + ".part")
        temporary.write_bytes(content)
        temporary.replace(original)
        if not working.exists():
            shutil.copyfile(original, working)
        with self.db:
            self.db.execute("UPDATE attachments SET status='ready',error=NULL WHERE id=?",
                            (attachment["id"],))
            self.register_file(attachment["user_id"], "original", attachment["path"], attachment["name"])

    def fail_attachment(self, attachment_id: str, error: str):
        with self.db:
            self.db.execute("UPDATE attachments SET status='failed',error=? WHERE id=?",
                            (error, attachment_id))

    def attachments(self, user_id: str, through_message: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT id,name,kind,path,status FROM attachments WHERE user_id=? AND message_id<=? AND status!='deleted' "
            "ORDER BY message_id DESC,item_index LIMIT 30", (user_id, through_message)
        )]

    def recent_messages(self, user_id: str, through_message: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT text,status FROM messages WHERE user_id=? AND id<=? AND text!='' "
            "ORDER BY id DESC LIMIT 10", (user_id, through_message)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_task(self, user_id: str, title: str) -> dict:
        task_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO tasks(id,user_id,title,session_id) VALUES (?,?,?,?)",
                            (task_id, user_id, title[:200], f"task-{user_id}-{task_id}"))
            self.db.execute("INSERT INTO task_numbers(task_id) VALUES (?)", (task_id,))
        return self.get_task(user_id, task_id)

    def get_task(self, user_id: str, task_id: str) -> dict:
        row = self.db.execute("SELECT tasks.*,task_numbers.number FROM tasks JOIN task_numbers "
                              "ON tasks.id=task_numbers.task_id WHERE tasks.id=? AND user_id=?", (task_id, user_id)).fetchone()
        if not row:
            raise ValueError("Task not found for this user")
        return dict(row)

    def find_tasks(self, user_id: str, query: str = "") -> list[dict]:
        escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        return [dict(row) for row in self.db.execute(
            "SELECT id,title,summary,updated_at,number FROM tasks JOIN task_numbers ON tasks.id=task_numbers.task_id "
            "WHERE user_id=? AND "
            "(title LIKE ? ESCAPE '!' OR summary LIKE ? ESCAPE '!') ORDER BY updated_at DESC LIMIT 20",
            (user_id, f"%{escaped}%", f"%{escaped}%"),
        )]

    def summarize_task(self, user_id: str, task_id: str, summary: str):
        self.get_task(user_id, task_id)
        with self.db:
            self.db.execute("UPDATE tasks SET summary=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (summary[:4000], task_id))

    def task_by_number(self, user_id: str, number: int) -> dict:
        row = self.db.execute("SELECT tasks.id FROM tasks JOIN task_numbers ON tasks.id=task_numbers.task_id "
                              "WHERE number=? AND user_id=?", (number, user_id)).fetchone()
        if not row:
            raise ValueError("Task not found for this user")
        return self.get_task(user_id, row["id"])

    def bind_task(self, user_id: str, message_id: int, task_id: str):
        self.get_task(user_id, task_id)
        owner = self.db.execute("SELECT user_id FROM messages WHERE id=?", (message_id,)).fetchone()
        if not owner or owner[0] != user_id:
            raise ValueError("Message does not belong to user")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO message_tasks VALUES (?,?)", (message_id, task_id))

    def task_for_message(self, user_id: str, message_id: int):
        row = self.db.execute("SELECT task_id FROM message_tasks JOIN messages ON messages.id=message_tasks.message_id "
                              "WHERE message_id=? AND user_id=?", (message_id, user_id)).fetchone()
        return self.get_task(user_id, row[0]) if row else None

    def task_state(self, user_id: str, task_id: str) -> str:
        self.get_task(user_id, task_id)
        waiting = self.db.execute("SELECT 1 FROM confirmations JOIN message_tasks USING(message_id) "
                                  "JOIN messages ON messages.id=message_tasks.message_id "
                                  "WHERE task_id=? AND confirmations.user_id=? AND confirmations.status='pending' "
                                  "AND messages.status='processing' LIMIT 1", (task_id, user_id)).fetchone()
        if waiting:
            return "waiting"
        rows = self.db.execute("SELECT status FROM messages JOIN message_tasks ON messages.id=message_tasks.message_id "
                               "WHERE task_id=? AND user_id=? ORDER BY messages.id DESC", (task_id, user_id)).fetchall()
        if any(row[0] == "processing" for row in rows):
            return "processing"
        return rows[0][0] if rows else "unknown"

    def register_file(self, user_id: str, source: str, path: str, name: str):
        self.db.execute("INSERT OR IGNORE INTO files(user_id,source,path,name) VALUES (?,?,?,?)",
                        (user_id, source, path, safe_filename(name)))

    def list_files(self, user_id: str, query: str = "") -> list[dict]:
        escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        records = [dict(row) for row in self.db.execute(
            "SELECT number,source,name,path,CASE WHEN source='original' THEN "
            "(SELECT MIN(messages.created_at) FROM attachments JOIN messages ON messages.id=attachments.message_id "
            "WHERE attachments.user_id=files.user_id AND attachments.path=files.path) ELSE "
            "(SELECT MIN(created_at) FROM outbox WHERE user_id=files.user_id AND content=files.path AND kind='file') "
            "END AS created_at FROM files WHERE user_id=? AND number NOT IN (SELECT number FROM deleted_files) "
            "AND name LIKE ? ESCAPE '!' ORDER BY number DESC LIMIT 20", (user_id, f"%{escaped}%"))]
        for record in records:
            record["size_bytes"] = None
            try:
                if record["source"] == "original":
                    path = confined_path(self.user_root(user_id) / "originals", record["path"])
                else:
                    path = confined_path(self.root / "outbound", str(confined_path(self.root, record["path"])))
                if path.is_file():
                    record["size_bytes"] = path.stat().st_size
            except (OSError, ValueError):
                pass
            del record["path"]
        return records

    def file_by_number(self, user_id: str, number: int) -> dict:
        row = self.db.execute("SELECT * FROM files WHERE user_id=? AND number=? "
                      "AND number NOT IN (SELECT number FROM deleted_files)", (user_id, number)).fetchone()
        if not row:
            raise ValueError("File not found for this user")
        return dict(row)

    def resend_file(self, user_id: str, message_id: int, number: int):
        record = self.file_by_number(user_id, number)
        if record["source"] == "original":
            source = confined_path(self.user_root(user_id) / "originals", record["path"])
        else:
            source = confined_path(self.root / "outbound", str(confined_path(self.root, record["path"])))
        if not source.is_file() or source.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("File unavailable or too large")
        if record["source"] == "original":
            destination = self.root / "outbound" / uuid.uuid4().hex / record["name"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            relative = str(destination.relative_to(self.root))
        else:
            relative = record["path"]
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO file_copies VALUES (?,?)", (number, relative))
        return self.enqueue(user_id, message_id, "file", relative, register=False)

    def file_numbers(self, user_id: str) -> list[int]:
        return [row[0] for row in self.db.execute("SELECT number FROM files WHERE user_id=? "
                                                "AND number NOT IN (SELECT number FROM deleted_files) ORDER BY number", (user_id,))]

    def delete_files(self, user_id: str, numbers: list[int]) -> dict:
        records = [self.file_by_number(user_id, number) for number in dict.fromkeys(numbers)]
        plans = []
        for record in records:
            copies = {row[0] for row in self.db.execute("SELECT path FROM file_copies WHERE number=?", (record["number"],))}
            paths = []
            if record["source"] == "original":
                root = self.user_root(user_id)
                paths.extend(confined_path(root / kind, record["path"]) for kind in ("originals", "workspace"))
            else:
                copies.add(record["path"])
            for relative in copies:
                if self.db.execute("SELECT 1 FROM outbox WHERE kind='file' AND content=? "
                                   "AND (status='sending' OR user_id!=?)", (relative, user_id)).fetchone():
                    raise ValueError("File is sending or shared with another owner")
                paths.append(confined_path(self.root / "outbound", str(confined_path(self.root, relative))))
            plans.append((record, copies, paths))
        deleted, failed = [], []
        for record, copies, paths in plans:
            try:
                for path in paths:
                    path.unlink(missing_ok=True)
            except OSError:
                failed.append(record["number"])
                continue
            with self.db:
                self.db.execute("INSERT INTO deleted_files VALUES (?)", (record["number"],))
                if record["source"] == "original":
                    self.db.execute("UPDATE attachments SET status='deleted' WHERE user_id=? AND path=?",
                                    (user_id, record["path"]))
                for relative in copies:
                    self.db.execute("UPDATE outbox SET status='cancelled',error=NULL WHERE user_id=? AND kind='file' "
                                    "AND content=? AND status IN ('pending','waiting_context','failed')", (user_id, relative))
            deleted.append(record["number"])
        return {"deleted": deleted, "failed": failed}

    def enqueue_text(self, user_id: str, message_id: int | None, content: str, *, state: str | None = None) -> str:
        task = self.task_for_message(user_id, message_id) if state and message_id is not None else None
        heading = f"[任务 {task['number']} · {state}]" if task else f"[对话 · {state}]" if state else "[系统]"
        prefix = heading + "\n"
        size = 3000 - len(prefix)
        identifiers = [self.enqueue(user_id, message_id, "text", prefix + content[offset:offset + size])
                       for offset in range(0, max(1, len(content)), size)]
        return identifiers[0]

    def enqueue(self, user_id: str, message_id: int | None, kind: str, content: str, *, register: bool = True) -> str:
        if kind == "text" and len(content) > 3000:
            identifiers = [self.enqueue(user_id, message_id, kind, content[offset:offset + 3000])
                           for offset in range(0, len(content), 3000)]
            return identifiers[0]
        delivery_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO outbox(id,user_id,message_id,kind,content) VALUES (?,?,?,?,?)",
                            (delivery_id, user_id, message_id, kind, content))
            if kind == "file" and register:
                self.register_file(user_id, "outbound", content, Path(content).name)
        return delivery_id

    def pending_deliveries(self) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT outbox.*,users.external_id FROM outbox JOIN users ON users.id=outbox.user_id "
            "WHERE outbox.status='pending' ORDER BY outbox.rowid"
        )]

    def mark_delivery(self, delivery_id: str, status: str, error: str | None = None):
        with self.db:
            self.db.execute("UPDATE outbox SET status=?,error=? WHERE id=?", (status, error, delivery_id))

    def delivery_ready(self, delivery_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM outbox current WHERE current.id=? AND NOT EXISTS ("
            "SELECT 1 FROM outbox prior WHERE prior.message_id=current.message_id "
            "AND prior.rowid<current.rowid AND prior.status!='sent')", (delivery_id,)
        ).fetchone() is not None