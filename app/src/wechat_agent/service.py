import asyncio
from datetime import datetime, timezone
import json
import re
import time

import httpx

from .agent import Agent
from .store import Store, confined_path
from .weixin import MEDIA_FIELDS, WeixinClient, WeixinError, media_items, message_text


def file_listing_entry(item):
    size = item["size_bytes"]
    if size is None:
        size_text = "大小未知"
    else:
        size_text = f"{size} B"
        for unit in ("KiB", "MiB", "GiB", "TiB"):
            if size < 1024:
                break
            size /= 1024
            size_text = f"{size:.1f} {unit}"
    try:
        timestamp = datetime.fromisoformat(item["created_at"]).replace(tzinfo=timezone.utc).astimezone()
        time_text = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        time_text = "时间未知"
    kind = "收到" if item["source"] == "original" else "生成"
    return f"[文件 {item['number']} · {kind}，{size_text}，{time_text}] {item['name']}"


def parse_command(text: str):
    text = text.strip()
    aliases = {
        "帮助": "help", "状态": "status", "查看状态": "status", "任务": "tasks",
        "查看任务": "tasks", "文件": "files", "查看文件": "files",
        "停止": "stop", "取消": "stop", "停一下": "stop",
        "全部停止": "stop_all", "全部取消": "stop_all",
        "暂停队列": "pause_queue", "恢复队列": "resume_queue",
        "模型": "models", "列举模型": "models", "查看模型": "models", "模型列表": "models",
        "/模型": "models", "/列举模型": "models", "/查看模型": "models", "/模型列表": "models",
        "/帮助": "help", "/help": "help", "/状态": "status", "/status": "status",
        "/任务": "tasks", "/文件": "files", "/取消": "stop", "/cancel": "stop",
        "/停止": "stop", "/全部取消": "stop_all", "/全部停止": "stop_all",
        "/暂停队列": "pause_queue", "/恢复队列": "resume_queue",
    }
    if text in aliases:
        return aliases[text], None, ""
    deletion = re.fullmatch(r"/?删除\s*\[\s*(\*|[1-9][0-9]{0,8}(?:\s*[,，]\s*[1-9][0-9]{0,8})*)\s*\]", text)
    if deletion:
        if deletion[1] == "*":
            return "delete_files", None, "*"
        numbers = list(dict.fromkeys(int(value.strip()) for value in re.split("[,，]", deletion[1])))
        return ("delete_files", None, numbers) if len(numbers) <= 100 else ("invalid_delete", None, "")
    if re.match(r"/?删除\s*\[", text):
        return "invalid_delete", None, ""
    model_match = re.fullmatch(r"/?切换模型\s+([A-Za-z0-9][A-Za-z0-9._:/-]{0,199})", text)
    if model_match:
        return "switch_model", None, model_match[1]
    match = re.fullmatch(r"/?(任务|文件|下载|继续任务|继续|同意|拒绝)\s+([1-9][0-9]{0,8})(?:\s+([^\r\n]+))?", text)
    if match:
        action, number, extra = match.groups()
        if extra and action not in ("继续任务", "继续"):
            return None
        return {"任务": "task", "文件": "file", "下载": "file", "继续任务": "continue",
                "继续": "continue", "同意": "approve", "拒绝": "reject"}[action], int(number), extra or ""
    return None


class Service:
    def __init__(self, store: Store, weixin: WeixinClient, credentials: dict, agent: Agent):
        self.store = store
        self.weixin = weixin
        self.agent = agent
        self.agent.ask = self.ask
        self.account = credentials["bot_id"]
        self.allowed_users = set(credentials.get("allowed_users", [credentials["user_id"]]))
        self.workers = {}
        self.questions = {}
        self.confirmations = {}
        self.running = {}
        self.shutdown = asyncio.Event()

    async def ask(self, user_id: str, message_id: int, question: str, *, approval: bool = False):
        if user_id in self.questions:
            raise ValueError("A question is already pending")
        future = asyncio.get_running_loop().create_future()
        with self.store.db:
            number = self.store.db.execute(
                "INSERT INTO confirmations(user_id,message_id) VALUES (?,?)", (user_id, message_id)
            ).lastrowid
        self.questions[user_id] = future
        self.confirmations[user_id] = {"number": number, "approval": approval}
        prompt = f"[确认 {number} · 等待确认] {question}\n回复：同意 {number} 或 拒绝 {number}"
        if not approval:
            prompt += "\n需要补充信息时，也可直接回复文字。"
        self.store.enqueue_text(user_id, message_id, prompt, state="等待确认")
        try:
            return await asyncio.wait_for(future, timeout=300)
        finally:
            self.questions.pop(user_id, None)
            self.confirmations.pop(user_id, None)
            with self.store.db:
                self.store.db.execute("UPDATE confirmations SET status='expired' WHERE number=? AND status='pending'", (number,))

    async def receive(self, raw: dict):
        if raw.get("message_type") != 1 or raw.get("from_user_id") not in self.allowed_users:
            return
        external_id = str(raw.get("message_id") or raw.get("seq") or "")
        if not external_id:
            return
        user_id = self.store.user(self.account, raw["from_user_id"])
        if raw.get("context_token"):
            self.store.set_setting("context:" + user_id, raw["context_token"])
            with self.store.db:
                self.store.db.execute(
                    "UPDATE outbox SET status='pending' WHERE user_id=? AND status='waiting_context'",
                    (user_id,),
                )
        text = message_text(raw)
        message_id = self.store.ingest(user_id, external_id, text, raw)
        row = self.store.db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        if row[0] != "pending":
            return
        command = parse_command(text)
        if command and await self.control(user_id, message_id, command):
            if media_items(raw):
                await self.download_attachments(user_id, message_id, raw)
            return
        if text and user_id in self.questions and not self.questions[user_id].done():
            confirmation = self.confirmations[user_id]
            if confirmation["approval"]:
                number = confirmation["number"]
                self.store.enqueue_text(user_id, message_id, f"当前等待操作确认，请发送 同意 {number} 或 拒绝 {number}。")
            else:
                self.questions[user_id].set_result(text)
                with self.store.db:
                    self.store.db.execute("UPDATE confirmations SET status='answered' WHERE number=?", (confirmation["number"],))
            if media_items(raw):
                await self.download_attachments(user_id, message_id, raw)
            self.store.mark_message(message_id, "done")
            return
        if text and self.request_must_wait(user_id, message_id) and not self.store.db.execute("SELECT 1 FROM outbox WHERE user_id=? AND message_id=? LIMIT 1",
                                               (user_id, message_id)).fetchone():
            paused = self.store.setting("queue_paused:" + user_id) == "1"
            self.store.enqueue_text(user_id, message_id,
                                    "已收到消息，正在排队。队列已暂停，恢复后开始处理。" if paused else
                                    "已收到消息，正在排队，轮到后开始处理。", state="排队中")

    def request_must_wait(self, user_id, message_id):
        return (self.store.setting("queue_paused:" + user_id) == "1"
                or self.files_busy(user_id)
                or self.store.db.execute("SELECT 1 FROM messages WHERE user_id=? AND id<? "
                                         "AND status IN ('pending','processing','cancelled_media') LIMIT 1",
                                         (user_id, message_id)).fetchone() is not None)

    def task_status(self, user_id, task):
        state = self.store.task_state(user_id, task["id"])
        return {"pending": "排队中", "processing": "处理中", "done": "已完成", "waiting": "等待确认",
            "failed": "失败", "interrupted": "已停止", "cancelled": "已停止",
            "cancelled_media": "已停止",
            "unknown": "状态未知"}.get(state, "状态未知")

    def cancel_queued(self, user_id, through_message):
        for message in self.store.pending(user_id):
            if message["id"] < through_message and message["text"]:
                status = "cancelled_media" if media_items(json.loads(message["payload"])) else "cancelled"
                self.store.mark_message(message["id"], status)

    def files_busy(self, user_id):
        worker = self.workers.get(user_id)
        return user_id in self.running or (worker is not None and not worker.done())

    def confirm_file_deletion(self, user_id, number, approved):
        request = self.store.db.execute(
            "SELECT confirmations.status,file_deletions.targets,expires_at>datetime('now') AS valid "
            "FROM confirmations JOIN file_deletions USING(number) WHERE number=? AND user_id=?",
            (number, user_id),
        ).fetchone()
        if not request:
            return None
        if request["status"] != "pending" or not request["valid"]:
            return "该删除确认已失效或已处理，请重新发送删除指令。"
        if not approved:
            status, reply = "rejected", "已取消删除，文件保留。"
        elif self.files_busy(user_id):
            status, reply = "blocked", "任务正在执行，未删除文件。请等待完成或停止任务后重新发送删除指令。"
        else:
            try:
                result = self.store.delete_files(user_id, json.loads(request["targets"]))
                status = "partial" if result["failed"] else "approved"
                reply = f"已删除 {len(result['deleted'])} 个文件。"
                if result["failed"]:
                    reply += "以下编号删除未完成，可能已有部分副本被删除，请重试：" + ", ".join(map(str, result["failed"]))
            except (ValueError, OSError):
                status, reply = "blocked", "文件状态已变化、正在发送或路径不可用，未完成删除。请重新查看文件列表后操作。"
        with self.store.db:
            self.store.db.execute("UPDATE confirmations SET status=? WHERE number=? AND user_id=?", (status, number, user_id))
        return reply

    async def control(self, user_id, message_id, command):
        action, number, extra = command
        if action != "continue":
            self.store.mark_message(message_id, "control")
        if action in ("stop", "stop_all"):
            worker = self.workers.get(user_id)
            active = worker is not None and not worker.done()
            if worker and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if action == "stop_all":
                self.cancel_queued(user_id, message_id)
                reply = "已请求停止当前执行，并清空待执行队列。"
            else:
                reply = "已请求停止当前执行，排队任务保留。" if active else "当前没有正在执行的任务，排队任务保留。"
            reply += "已完成的操作不会撤销，独立子进程可能仍需手动停止。"
        elif action in ("pause_queue", "resume_queue"):
            self.store.set_setting("queue_paused:" + user_id, "1" if action == "pause_queue" else "0")
            reply = "队列已暂停，当前执行不受影响。" if action == "pause_queue" else "队列已恢复。"
        elif action == "status":
            active = user_id in self.workers and not self.workers[user_id].done()
            count = self.store.db.execute("SELECT COUNT(*) FROM messages WHERE user_id=? "
                                          "AND status='pending' AND text!='' AND id!=?", (user_id, message_id)).fetchone()[0]
            paused = self.store.setting("queue_paused:" + user_id) == "1"
            reply = ("正在处理任务。" if active else "当前没有正在执行的任务。")
            reply += f"\n排队：{count} 项\n队列：{'已暂停' if paused else '正常'}"
            reply += f"\n后续请求模型：{self.store.setting('model:' + user_id, self.agent.model)}"
            running = self.running.get(user_id)
            if running:
                task = self.store.task_for_message(user_id, running["message_id"])
                elapsed = int(time.monotonic() - running["started"])
                reply += f"\n已运行：{elapsed // 60} 分 {elapsed % 60} 秒"
                reply += f"\n本轮模型：{running['model']}"
                reply += f"\n[任务 {task['number']} · {self.task_status(user_id, task)}] {task['title'][:80]}" if task else "\n阶段：接收资料或分析请求"
                recent = self.store.db.execute("SELECT created_at FROM outbox WHERE message_id=? ORDER BY rowid DESC LIMIT 1",
                                               (running["message_id"],)).fetchone()
                if recent:
                    reply += f"\n最近通知时间（UTC）：{recent[0]}"
            if user_id in self.confirmations:
                reply += f"\n[确认 {self.confirmations[user_id]['number']} · 等待确认] 请回复对应编号。"
            deletion = self.store.db.execute("SELECT number FROM confirmations JOIN file_deletions USING(number) "
                                             "WHERE user_id=? AND status='pending' AND expires_at>datetime('now')", (user_id,)).fetchone()
            if deletion:
                reply += f"\n[确认 {deletion[0]} · 等待确认] 删除文件"
        elif action == "tasks":
            tasks = self.store.find_tasks(user_id)
            reply = "最近任务（最多 20 项）：\n" + "\n".join(
                f"[任务 {task['number']} · {self.task_status(user_id, task)}] {task['title'][:80]}" for task in tasks
            ) if tasks else "暂无任务记录。"
            if tasks:
                reply += "\n发送 任务 编号 查看详情，或 继续任务 编号 追加要求。"
        elif action in ("task", "continue"):
            try:
                task = self.store.task_by_number(user_id, number)
            except ValueError:
                reply = "未找到该任务，请发送 任务 查看可用编号。"
            else:
                if action == "continue":
                    if self.store.task_for_message(user_id, message_id):
                        return True
                    self.store.bind_task(user_id, message_id, task["id"])
                    if not self.request_must_wait(user_id, message_id):
                        return True
                    paused = self.store.setting("queue_paused:" + user_id) == "1"
                    reply = f"已排队继续任务 {number}：{task['title'][:80]}。"
                    reply += "队列已暂停，发送 恢复队列 后执行。" if paused else "将基于已有上下文和成果继续，不撤销已完成操作。"
                    self.store.enqueue_text(user_id, message_id, reply, state="排队中")
                    return True
                reply = (f"[任务 {number} · {self.task_status(user_id, task)}] {task['title'][:120]}"
                         f"\n更新时间（UTC）：{task['updated_at']}\n摘要：{task['summary'][:1600] or '暂无摘要'}"
                         f"\n继续处理：继续任务 {number} 你的要求")
        elif action == "files":
            files = self.store.list_files(user_id)
            reply = "最近文件（最多 20 项，时间为电脑本地时间）：\n" + "\n".join(
                file_listing_entry(item) for item in files
            ) if files else "暂无可取回的文件。"
            if files:
                reply += "\n发送 文件 编号 获取文件，或 删除 [编号1, 编号2] 删除文件。"
        elif action == "invalid_delete":
            reply = "删除格式：删除 [编号1, 编号2] 或 删除 [*]。请填入实际数字编号，每次最多 100 个指定编号。"
        elif action == "delete_files":
            if self.files_busy(user_id):
                reply = "任务正在执行，请等待完成或停止任务后再删除文件。"
            else:
                numbers = self.store.file_numbers(user_id) if extra == "*" else extra
                try:
                    records = [self.store.file_by_number(user_id, identifier) for identifier in numbers]
                except ValueError:
                    reply = "存在无效或不属于你的文件编号，未创建删除请求。请发送 文件 查看列表。"
                else:
                    if not records:
                        reply = "当前没有可删除的文件。"
                    else:
                        with self.store.db:
                            self.store.db.execute("UPDATE confirmations SET status='expired' WHERE user_id=? "
                                                  "AND status='pending' AND number IN (SELECT number FROM file_deletions)", (user_id,))
                            confirmation = self.store.db.execute("INSERT INTO confirmations(user_id,message_id) VALUES (?,?)",
                                                                 (user_id, message_id)).lastrowid
                            self.store.db.execute("INSERT INTO file_deletions VALUES (?,?,datetime('now','+5 minutes'))",
                                                  (confirmation, json.dumps(numbers)))
                        listing = "\n".join(f"[文件 {record['number']} · 待删除] {record['name']}" for record in records[:10])
                        reply = (f"[确认 {confirmation} · 等待确认] 将删除文件库中的 {len(records)} 个文件（以下最多显示 10 项）。\n{listing}\n"
                                 "范围：原始上传文件及上传工作副本、已登记输出/发送副本。生成源文件和其他任务工作区文件保留。\n"
                                 "不删除登录、任务历史，也不能撤回微信中已收到的文件。删除不可撤销，可能影响旧任务继续处理。\n"
                                 f"五分钟内发送 同意 {confirmation} 执行，或 拒绝 {confirmation} 取消。之后新收到的文件不受影响。")
        elif action == "file":
            try:
                self.store.resend_file(user_id, message_id, number)
            except (ValueError, OSError):
                reply = "文件不存在、不可用或超过大小限制。请发送 文件 查看可用编号。"
            else:
                self.store.mark_message(message_id, "done")
                return True
        elif action in ("approve", "reject"):
            deletion_reply = self.confirm_file_deletion(user_id, number, action == "approve")
            if deletion_reply is not None:
                status = self.store.db.execute("SELECT status FROM confirmations WHERE number=? AND user_id=?",
                                               (number, user_id)).fetchone()[0]
                label = {"approved": "已完成", "rejected": "已拒绝", "partial": "部分失败",
                         "blocked": "未执行", "expired": "已失效"}.get(status, "已失效")
                self.store.enqueue_text(user_id, message_id, f"[确认 {number} · {label}] {deletion_reply}")
                self.store.mark_message(message_id, "done")
                return True
            confirmation = self.confirmations.get(user_id)
            future = self.questions.get(user_id)
            if not confirmation or confirmation["number"] != number or not future or future.done():
                reply = "该确认请求不存在或已失效。发送 状态 查看当前等待项。"
            else:
                with self.store.db:
                    self.store.db.execute("UPDATE confirmations SET status=? WHERE number=? AND user_id=?",
                                          ("approved" if action == "approve" else "rejected", number, user_id))
                future.set_result("YES" if action == "approve" else "NO")
                reply = f"[确认 {number} · {'已同意' if action == 'approve' else '已拒绝'}] 已记录你的选择。"
        elif action in ("models", "switch_model"):
            try:
                available = await asyncio.wait_for(self.agent.available_models(user_id), timeout=30)
            except Exception as error:
                reply = ("暂时无法获取可用模型，模型设置未改变。请联系管理员检查 GitHub Token 是否过期、"
                         f"Copilot 授权及服务连接。错误类型：{type(error).__name__}")
            else:
                known = json.loads(self.store.setting("model_numbers:" + user_id, "[]"))
                for model_id in sorted(set(available)):
                    if model_id not in known:
                        known.append(model_id)
                self.store.set_setting("model_numbers:" + user_id, json.dumps(known))
                current = self.store.setting("model:" + user_id, self.agent.model)
                if action == "models":
                    running = self.running.get(user_id)
                    active_model = running["model"] if running else None
                    reply = f"后续请求模型：{current}" if running else f"当前使用模型：{current}（空闲）"
                    if running:
                        reply += f"\n正在使用模型：{active_model}"
                    entries = []
                    for index, model_id in enumerate(known, 1):
                        if model_id not in available:
                            continue
                        labels = []
                        if model_id == active_model:
                            labels.append("正在使用")
                        if model_id == current:
                            labels.append("后续请求" if running else "当前使用")
                        marker = "，".join(labels) if labels else "可用"
                        entries.append(f"[模型 {index} · {marker}] {model_id}")
                    reply += "\n可用模型：\n" + "\n".join(entries) if entries else "\n当前账号未返回可用模型，设置未改变。"
                    reply += "\n发送 切换模型 编号，填写列表中的实际编号；也可填写完整模型ID。切换不打断当前执行。"
                else:
                    if extra.isdigit():
                        selected = known[int(extra) - 1] if len(extra) <= 9 and 1 <= int(extra) <= len(known) else None
                    else:
                        selected = extra
                    if selected not in available:
                        reply = "该模型编号或 ID 当前不可用，设置未改变。请发送 模型 查看列表。"
                    else:
                        self.store.set_setting("model:" + user_id, selected)
                        reply = (f"[模型 {known.index(selected) + 1} · 后续请求] {selected}\n"
                                 "后续请求已切换，当前执行不受影响，其他用户的设置不变。")
        elif action == "help":
            reply = ("状态：查看当前执行和队列\n任务：列出任务\n任务 编号：查看任务详情\n"
                     "文件：列出文件\n文件 编号：获取文件\n继续任务 编号：继续已有任务\n"
                     "删除 [编号1, 编号2]：删除指定文件，需确认\n删除 [*]：清空文件库，需确认\n"
                     "继续任务 编号 你的要求：追加要求\n停止：仅停止当前执行\n"
                     "全部停止：停止并清空队列\n暂停队列：暂不启动下一项\n恢复队列：恢复调度\n"
                     "模型：列举可用模型并标注当前使用的模型\n切换模型 编号：切换后续请求使用的模型\n"
                     "同意 编号 / 拒绝 编号：回答对应确认请求\n请将“编号”替换为列表或确认消息中的实际数字。")
        else:
            return False
        self.store.enqueue_text(user_id, message_id, reply)
        self.store.mark_message(message_id, "done")
        return True

    async def download_attachments(self, user_id, message_id, raw):
        for index, item in enumerate(media_items(raw)):
            kind, field, extension = MEDIA_FIELDS[item["type"]]
            attachment = self.store.allocate_attachment(
                user_id, message_id, index, item.get(field, {}).get("file_name") or f"attachment{extension}", kind,
            )
            if attachment["status"] in ("ready", "deleted"):
                continue
            try:
                self.store.save_attachment(attachment, await self.weixin.download(item))
            except Exception as error:
                self.store.fail_attachment(attachment["id"], type(error).__name__)

    async def process_user(self, user_id):
        while not self.shutdown.is_set():
            pending = self.store.pending(user_id)
            if self.store.setting("queue_paused:" + user_id) == "1":
                pending = [message for message in pending if not message["text"] or message["status"] == "cancelled_media"]
            if not pending:
                return
            message = pending[0]
            cancelled_media = message["status"] == "cancelled_media"
            command = None if cancelled_media else parse_command(message["text"])
            if command and not self.store.task_for_message(user_id, message["id"]):
                if command[0] in ("stop", "stop_all"):
                    if command[0] == "stop_all":
                        self.cancel_queued(user_id, message["id"])
                    self.store.mark_message(message["id"], "done")
                    await self.download_attachments(user_id, message["id"], json.loads(message["payload"]))
                    self.store.enqueue_text(user_id, message["id"], "已处理停止请求；重启前的执行不会自动重放。")
                    continue
                if await self.control(user_id, message["id"], command):
                    await self.download_attachments(user_id, message["id"], json.loads(message["payload"]))
                    if command[0] != "continue" or not self.store.task_for_message(user_id, message["id"]):
                        continue
            if not cancelled_media:
                self.store.mark_message(message["id"], "processing")
            self.running[user_id] = {"message_id": message["id"], "started": time.monotonic(),
                                     "model": self.store.setting("model:" + user_id, self.agent.model)}
            heartbeat = None
            try:
                if message["text"] and not cancelled_media:
                    self.store.enqueue_text(user_id, message["id"], "收到请求，开始处理。", state="处理中")
                await self.download_attachments(user_id, message["id"], json.loads(message["payload"]))
                if message["text"] and not cancelled_media:
                    heartbeat = asyncio.create_task(self.task_heartbeat(user_id, message["id"]))
                    await asyncio.wait_for(self.agent.handle(user_id, message, model=self.running[user_id]["model"]), timeout=1200)
                self.store.mark_message(message["id"], "cancelled" if cancelled_media else "done")
            except asyncio.CancelledError:
                self.store.mark_message(message["id"], "cancelled_media" if cancelled_media else "interrupted")
                if message["text"] and not cancelled_media:
                    self.store.enqueue_text(user_id, message["id"], "本轮处理已停止，已完成的操作不会撤销。", state="已停止")
                raise
            except Exception as error:
                self.store.mark_message(message["id"], "failed", type(error).__name__)
                self.store.enqueue_text(user_id, message["id"],
                                   "本次处理未完成，资料已保留。请联系管理员检查 GitHub Token 是否过期、"
                                   "Copilot 授权及服务连接，恢复后再继续。错误类型：" + type(error).__name__, state="失败")
            finally:
                self.running.pop(user_id, None)
                if heartbeat:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)

    async def task_heartbeat(self, user_id, message_id):
        while True:
            await asyncio.sleep(60)
            recent = self.store.db.execute(
                "SELECT 1 FROM outbox WHERE user_id=? AND message_id=? "
                "AND created_at>=datetime('now','-55 seconds') LIMIT 1", (user_id, message_id)
            ).fetchone()
            if not recent:
                waiting = user_id in self.questions and not self.questions[user_id].done()
                self.store.enqueue_text(user_id, message_id,
                                        "正在等待你的确认或补充信息。" if waiting else
                                        "任务仍在运行，暂未完成。你可以发送 状态 查看状态，或 停止 结束当前执行。",
                                        state="等待确认" if waiting else "处理中")

    def start_pending_workers(self):
        users = self.store.db.execute("SELECT DISTINCT user_id FROM messages WHERE status IN ('pending','cancelled_media')").fetchall()
        for row in users:
            user_id = row[0]
            if self.store.setting("queue_paused:" + user_id) == "1" and not any(
                not message["text"] or message["status"] == "cancelled_media" for message in self.store.pending(user_id)
            ):
                continue
            if user_id not in self.workers or self.workers[user_id].done():
                self.workers[user_id] = asyncio.create_task(self.process_user(user_id))

    async def flush_outbox(self):
        for delivery in self.store.pending_deliveries()[:20]:
            state = self.store.db.execute("SELECT status FROM outbox WHERE id=?", (delivery["id"],)).fetchone()
            if not state or state[0] != "pending":
                continue
            if not self.store.delivery_ready(delivery["id"]):
                continue
            context = self.store.setting("context:" + delivery["user_id"])
            if not context:
                self.store.mark_delivery(delivery["id"], "waiting_context")
                continue
            self.store.mark_delivery(delivery["id"], "sending")
            try:
                if delivery["kind"] == "file":
                    path = confined_path(self.store.root / "outbound",
                                         str(confined_path(self.store.root, delivery["content"])))
                    item = await self.weixin.upload(path, delivery["external_id"])
                else:
                    item = {"type": 1, "text_item": {"text": delivery["content"]}}
                await self.weixin.send_items(delivery["external_id"], context, [item], delivery["id"])
                self.store.mark_delivery(delivery["id"], "sent")
            except httpx.ConnectError:
                attempt_key = "delivery_attempt:" + delivery["id"]
                attempts = int(self.store.setting(attempt_key, "0")) + 1
                self.store.set_setting(attempt_key, str(attempts))
                self.store.mark_delivery(delivery["id"], "pending" if attempts < 3 else "failed", "ConnectError")
            except httpx.TimeoutException:
                self.store.mark_delivery(delivery["id"], "uncertain", "TimeoutError")
            except WeixinError as error:
                status = "waiting_context" if "-14" in str(error) else "failed"
                self.store.mark_delivery(delivery["id"], status, type(error).__name__)
                print("WeChat delivery rejected. Contact the administrator to check WeChat login expiry, "
                      "reply context and account permissions. Error type:", type(error).__name__, flush=True)
            except Exception as error:
                self.store.mark_delivery(delivery["id"], "failed", type(error).__name__)
                print("WeChat delivery failed. Contact the administrator to check WeChat login expiry "
                      "and service connectivity. Error type:", type(error).__name__, flush=True)

    async def poll_loop(self):
        while not self.shutdown.is_set():
            try:
                response = await self.weixin.updates(self.store.setting("cursor:" + self.account))
                for raw in response.get("msgs", []):
                    await self.receive(raw)
                if response.get("get_updates_buf"):
                    self.store.set_setting("cursor:" + self.account, response["get_updates_buf"])
                self.start_pending_workers()
                if not response.get("msgs"):
                    await asyncio.sleep(1)
            except Exception as error:
                print("WeChat receiving failed. Contact the administrator to check WeChat login expiry "
                      "and service connectivity. If login has expired, stop the service and scan again. "
                      "WeChat notifications may be unavailable. Error type:", type(error).__name__, flush=True)
                await asyncio.sleep(5)

    async def delivery_loop(self):
        while not self.shutdown.is_set():
            await self.flush_outbox()
            await asyncio.sleep(1)

    async def watch_stop(self, stop_requested):
        while not self.shutdown.is_set():
            if stop_requested():
                self.shutdown.set()
                return
            try:
                await asyncio.wait_for(self.shutdown.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def run(self, stop_requested=None):
        self.store.recover()
        self.start_pending_workers()
        print("WeChat assistant running. Only the bound account is allowed by default.", flush=True)
        loops = [asyncio.create_task(self.poll_loop()), asyncio.create_task(self.delivery_loop())]
        if stop_requested:
            loops.append(asyncio.create_task(self.watch_stop(stop_requested)))
        try:
            completed, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        finally:
            self.shutdown.set()
            for task in [*loops, *self.workers.values()]:
                task.cancel()
            await asyncio.gather(*loops, *self.workers.values(), return_exceptions=True)
            for future in self.questions.values():
                future.cancel()
            await self.weixin.close()