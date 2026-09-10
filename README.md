# WeChat Copilot Agent

Local-first WeChat assistant using the official GitHub Copilot SDK. Default model:
`gpt-6-astra`. Each user can explicitly choose another available model with a Chinese
command. Model availability is checked; there is no silent fallback.

## Setup

Python 3.11+ and uv are required. Run commands from this repository root:

```powershell
uv sync --project app
uv run --project app python -m copilot download-runtime
uv run --project app pytest app/tests -q
```

The application environment is separate from user workspaces. Each user workspace
gets a small Python project manifest; uv can create its environment on demand.
Independent projects can keep their own environments. No Conda base changes are needed.

## Authentication

Place a supported GitHub user token in the existing local `token.txt`, or supply a
private file outside the repository. Never commit or paste credentials into chat.
The launchers use `token.txt` in the project root. It is intentionally excluded from
Git; transfer it separately over a secure channel when deploying to another machine.
Do not include it in public archives, container images, or model workspaces.
The application reads credentials locally. Diagnostics do not print token values,
identity details, or raw exception messages. A valid GitHub token still needs the
appropriate Copilot entitlement and permissions. Multi-user licensing must be checked
before expanding from the owner account.

```powershell
uv run --project app wechat-agent doctor --token-file token.txt --state-dir app/data/diagnostic
uv run --project app wechat-agent doctor --token-file token.txt --state-dir app/data/diagnostic --smoke --report-file app/data/doctor-report.json
```

The smoke check makes two synthetic model requests, restarting the runtime between
them to check persistence. It has no filesystem or shell tools.

## WeChat Login

Start the server first, then run the login launcher to open a private browser window.
Scan the QR code with WeChat and confirm on your phone. The page checks confirmation
automatically; the server saves the account and connects it without another start command.
No account name or profile selection is required. Refresh expired QR codes with **New QR code**.
Closing the browser does not stop the server. If you close it before confirmation has been
processed, reopen the login launcher and scan again. Cancel discards the pending login.
The adapter connects directly to WeChat iLink HTTPS endpoints, without CowAgent
hosting, a public callback server, client hooks, or a third-party model proxy.
Account eligibility and platform message restrictions still apply.

## Run

### Windows Double-Click Scripts

Double-click these scripts in the project root; VS Code is not required:

| Script | Purpose |
| --- | --- |
| [start-service.cmd](start-service.cmd) | Start the persistent server, even with no logged-in accounts. |
| [stop-service.cmd](stop-service.cmd) | Stop the server and all account connections, retaining login and data. |
| [login-wechat.cmd](login-wechat.cmd) | Open the browser QR sign-in window; automatically connect after confirmation. |

After startup succeeds, the launcher window and VS Code can be closed. The service has
no console window: its input is disconnected and account/server diagnostics go to
`app/data/server/service.log`. Use `stop-service.cmd` to stop it.
Stop old standalone instances once before migrating to the persistent server.
The scripts require uv and the project dependencies. Start and stop control the entire
project instance; neither asks for a profile:

```powershell
.\start-service.cmd
.\login-wechat.cmd
.\stop-service.cmd
```

### Linux Scripts

Use a Linux distribution and architecture supported by the installed Copilot SDK runtime.
Run as a dedicated, non-root user with Python 3.11+ and uv available. Recreate the Python
environment on Linux; never copy the Windows `.venv` or Windows runtime executables.
From the project root, after securely placing the token in `token.txt`:

```sh
umask 077
chmod 600 token.txt
uv sync --locked --project app
uv run --project app python -m copilot download-runtime
uv run --project app pytest app/tests -q
sh start-service.sh
sh login-wechat.sh
sh stop-service.sh
```

The three scripts can be called from any working directory. Using `sh` avoids requiring
executable file permissions; alternatively, use `chmod +x start-service.sh stop-service.sh
login-wechat.sh` and invoke them with `./`. Shell scripts use LF line endings.

Linux with a desktop opens the same browser login. On a headless server, use
`sh login-wechat.sh --terminal` after starting the server. For a separate account in this
terminal-only fallback, add `--profile family`. The default is the original account.
The fallback renders the QR code in the terminal. Use a wide terminal on another screen.
If rendering is unsuitable, retrieve the temporary `login.png` over authenticated SSH/SFTP
and open it locally. Do not host it on a public web server or publish it in logs. Press
Enter after phone confirmation; use `R` for a new QR or `Q` to cancel.

Linux startup creates a detached process with standard input disconnected and output
appended to `app/data/server/service.log`.
New files are private to the current OS user through umask 077. Existing files keep their
current permissions; restrict migrated data and credentials separately. Treat logs as
private and monitor their size; automatic log rotation is not included.

The process normally survives SSH disconnects, but server policies can terminate user
processes on logout. These scripts do not configure systemd or boot startup. For unattended
hosting, configure a service manager under the dedicated user and run
`uv run --project app python -m wechat_agent.server --root /absolute/project/path`
in the foreground, not the detached start script.
No root privileges or automatic system configuration are requested by these scripts.

On both platforms this is detached execution, not a tmux-style reattachable terminal.
Follow the private log for output. Closing a launcher or terminal normally leaves the
service running, but shutdown, sleep, OS logout policies, administrative process cleanup,
or process failure can still interrupt it. The server retries failed account workers after
60 seconds; it cannot restart itself after a crash. Use an OS service manager for that.

### Profiles and Login Renewal

Start and stop are server-wide commands for this project instance, not per-user commands.
The persistent manager remains alive with no accounts, discovers saved logins every half
second and runs isolated account workers as asynchronous tasks. Existing per-account data
and Copilot sessions are preserved. Other project copies and custom data directories are
outside its scope. `--profile` is rejected for start and stop.

Browser login identifies the account from the confirmed WeChat bot/user identity. The first
account uses the default paths; additional accounts get automatically named directories
under `app/data/profiles/`. Signing into an existing account renews that account's credentials
and preserves its allowlist. Its current work is interrupted while reconnecting; other
accounts continue. Failed or cancelled scans leave saved credentials unchanged. This does
not add contacts to an existing bot's allowlist. Accounts share the project-root GitHub token;
review multi-user licensing. Signing in does not prove Copilot entitlement or Token validity.

The legacy `--terminal` login still offers reuse or rescan and supports `--profile`. Stop
the server before renewing an active account with that fallback, then start it again.
The browser flow supports renewal while the server is running.

The login page binds only to `127.0.0.1` on an available port. The launcher reads a private
capability from `app/data/server/web.json` and passes it in a URL fragment; the page removes
the fragment and holds it in tab-local session storage. APIs require that capability and
reject foreign Host/Origin headers. QR sessions expire after ten minutes; abandoned scan
files are removed on the next login request, server restart or shutdown. GitHub and WeChat
tokens never go to the browser. Treat the capability and QR images as private credentials.
Do not expose this local HTTP service through a public proxy. Remote browser access needs
an authenticated SSH tunnel and private capability transfer; there is no public login portal.

Retaining credentials is not an online validity check; expired credentials require a new
scan. Stop submits exit requests for all accounts, not proof of completed cleanup. Active work is
interrupted, completed actions are not undone, queued requests remain saved, and detached
task subprocesses are not guaranteed to stop. No launcher changes sleep or execution policy.

### Network and Migration

The phone and server do not need to share a LAN or VPN. Each connects outbound to its
respective services. The server must reach WeChat iLink and its media CDN over HTTPS,
GitHub/Copilot authentication and inference services, and runtime/package download hosts
during setup. DNS, certificates, firewall/proxy rules and regional availability must allow
those connections; merely having an Internet connection does not guarantee reachability.
The bot uses long polling, not an inbound webhook, so it needs no public domain, public
HTTP port, router port forwarding, or phone-to-server connection. The login window uses
a local-only HTTP listener. SSH is only for server
administration and is not required by the bot protocol. The server must remain powered,
awake and connected for messages and tasks to be processed.

Stop the Windows instance before moving credentials or data, and do not poll the same bot
from both machines. Back up the complete stopped data directory, including SQLite state,
and transfer it and the token securely outside Git. Reinstall dependencies and runtime
on Linux and verify authentication there; reuse of WeChat credentials depends on platform
validity, so be prepared to scan again. Historical SDK sessions or artifacts can contain
Windows-specific absolute paths, commands and environments: seamless cross-OS task resume
is not guaranteed. Retain the backup and start a new task if an old session cannot resume.

### Command Line

```powershell
uv run --project app wechat-agent serve --token-file token.txt
uv run --project app wechat-agent stop --data-dir app/data/assistant
```

These low-level CLI commands operate on a single data directory (the default account
unless overridden), unlike the server-wide start/stop launchers above.
Keep this process and the computer running. Use Ctrl+C to stop. Run only one service
instance for a given data directory; a process lock prevents duplicate instances.
The stop command requests cleanup of that instance without killing unrelated processes.
There is no public web interface. The manager's browser login listens only on loopback.
Initially only the user bound during QR login is accepted.

```powershell
uv run --project app wechat-agent status
```

Status shows counts and error types only, not message contents or credentials.

Copilot request and model-list failures tell the WeChat user to contact the administrator
to check GitHub Token expiry, Copilot authorization and connectivity. These messages do
not claim every failure is an expired token and never include raw exception details.
WeChat receive/delivery failures write administrator guidance to the private server log
(or the account log for legacy standalone workers). An expired WeChat login may prevent both receiving and sending messages,
so an in-chat expiry notification cannot be guaranteed. The administrator must monitor
the log and scan again through the login launcher when required. Replacing the GitHub token also
requires a service restart; there is no proactive expiry monitor or external alert channel.

## Interaction

- Both agent roles can query the read-only `get_capabilities` tool for the application's
  delivery, execution and permission boundaries. Normal text replies are already queued
  to the current WeChat conversation; asking for a message to yourself now does not require
  desktop control or a separate messaging integration. File/script work is delegated to
  the task worker rather than rejected merely because the reception agent has no shell.
  Task selection binds the current request immediately, even if the reception response
  later fails. Timers, arbitrary WeChat recipients, browser automation, MCP and Skills
  remain unavailable. Authorization never creates a missing integration or overrides
  workspace restrictions. Prompt guidance improves routing but is not a guarantee of
  model behavior or proof of message delivery.
- Incoming ordinary text queues a receipt marked `[对话 · 排队中]` only when earlier work
  exists for that user, a worker is active, or the queue is paused. Idle requests skip
  the queued notice. Explicit task continuation follows the same rule and uses its task number. Processing emits
  a start notice before attachment downloads or model calls. Repeated delivery of the same
  incoming message does not repeat its receipt, including across restarts. Commands return
  their own replies; confirmation answers are not queued as new work. Attachments alone
  remain silent. These notices use the normal delivery queue, so WeChat context, network
  availability and earlier failed deliveries can still delay actual arrival.
- Task text replies use `[任务 n · 状态]` followed by the reply body. Task list entries
  use `[任务 n · 状态] title` on one line. States include 排队中, 处理中, 等待确认,
  已完成, 已停止 and 失败; unrecorded historical states show 状态未知. Completed means
  this processing round finished, not that every future project requirement is complete.
  Ordinary answers without an assigned task use `[对话 · 状态]`; command replies use
  `[系统]`. Every chunk of a long reply retains its header. File payloads are unchanged.
  Progress describes public actions only, never private model reasoning.
- Images, files, videos, and voice attachments are stored without invoking Copilot.
- Text invokes the user's Copilot reception session. Simple questions get direct
  answers; the agent can search, create, or continue persistent task sessions.
- People and projects are mentioned in task titles/summaries, not merged into one
  permanent session per person. Search is currently literal substring search.
- The reception session has only task-index tools. A separate user-local execution
  runtime handles files, commands, short progress updates, questions, and outputs.
- Material descriptions and collection instructions are interpreted by Copilot.
  Collection mode is currently conversational, not a separate deterministic state machine.
- Chinese commands below are handled directly, without invoking Copilot inference.
  Matching uses the entire message after trimming whitespace, never words inside ordinary
  sentences, attachment contents, or quoted messages. Existing slash commands remain aliases.
- Cancellation does not undo completed changes or retract queued/sent results, and cannot
  guarantee termination of independently detached child processes. Queue cancellation
  still preserves incoming attachments. Pausing the queue persists across restarts;
  attachments continue to be stored while text work is paused.
- Authorization requests require a matching numbered approval/rejection. Other questions
  can accept a plain-text answer. Queries and task commands never count as answers.
  Ask-user requests expire after five minutes or service restart. A running task has a
  twenty-minute overall limit.
- User work is serialized in this MVP. Further ordinary questions queue behind an
  active task; independent simultaneous chat and execution is a later enhancement.
- Progress is short public action information, rate-limited to one tool update per
  twenty seconds. If no update has been queued recently, an active text task emits
  a running-status notice approximately once a minute. This is a liveness notice,
  not a claim of newly completed work. Private reasoning events are not forwarded.
- Outputs are snapshotted before queuing for delivery. Long text is split. Outbound
  media currently uses generic file messages, including generated images/videos.

### Chinese Commands

| Message | Action |
| --- | --- |
| `帮助` | Show the command menu. |
| `状态` / `查看状态` | Activity, elapsed time, current task/model, queue and pending confirmation. |
| `任务` / `查看任务` | List up to 20 recent tasks with stable numeric IDs and recorded states. |
| `任务 编号` | Show the specified task, its latest summary and state. |
| `文件` / `查看文件` | List up to 20 recent uploaded/generated files with stable numeric IDs. |
| `文件 编号` / `下载 编号` | Queue the specified file for delivery to the current user. |
| `删除 [编号1, 编号2]` | Request deletion of selected library files; numbered confirmation is required. |
| `删除 [*]` | Request deletion of all current library files, including entries beyond the recent list. |
| `继续任务 编号` | Queue continuation in the specified task's saved session, bypassing automatic task selection. |
| `继续任务 编号 你的要求` | Continue the specified task with additional requirements. |
| `停止` / `取消` / `停一下` | Request cancellation of current execution only; queued work remains eligible to run. |
| `全部停止` / `全部取消` | Request cancellation of current execution and cancel queued text work. |
| `暂停队列` | Finish current execution but do not start the next text request. |
| `恢复队列` | Resume queued text requests. |
| `同意 编号` / `拒绝 编号` | Answer the specified confirmation, only while it is pending for this user. |
| `模型` / `列举模型` / `查看模型` / `模型列表` | List available models, marking the running model and the selection for subsequent requests separately. |
| `切换模型 编号` | Validate and select a model from this user's stable model list. |
| `切换模型 gpt-6-astra` | Select an available model by its exact model ID. |

`编号` is a placeholder; replace it with the numeric ID shown in replies. IDs remain stable after restart
and all lookups check ownership. Task, file, confirmation and model IDs are separate
namespaces. Old tasks and file deliveries are indexed on upgrade; old tasks without
execution associations display an unknown historical state, not a fabricated completion
state. File lists contain retained uploads and explicitly queued output snapshots, not
every file in the workspace. An older known number can still be queried directly.
Numbered entries use `[type number · brief information] content`: file entries are
`[文件 number · source, size, time] name`, model entries are `[模型 number · state] model ID`,
and confirmations are `[确认 number · state] question/result`. Deletion previews label
each file as 待删除. Existing numeric namespaces and command syntax are unchanged;
system messages without their own IDs retain `[系统]` rather than inventing numbers.
Sizes come from retained originals or output snapshots, not editable working copies.
Received time is the locally recorded incoming-message time; generated time is the first
output snapshot registration time, not the source file's filesystem creation time. Times
are displayed in the server's local timezone. Missing metadata is explicitly unknown.

Deletion accepts square brackets and comma-separated numeric IDs (ASCII or Chinese commas),
with up to 100 explicit IDs per request. `[*]` captures all of this user's current library
IDs, not just the latest 20; later arrivals are excluded. A new deletion request replaces
that user's previous pending deletion request. Confirm with the supplied confirmation ID
within five minutes. Rejection, expiry, restart, or an already-used confirmation cannot
delete files. Active tasks and files currently sending block deletion.

Deletion removes retained uploads and their known inbox working copies, registered output
snapshots, and resend copies tracked since this feature was introduced. It cancels pending
deliveries of those copies and keeps deletion markers so files do not return after restart.
Generated source files elsewhere in the workspace, untracked historical resend copies,
task history, credentials, backups and already-delivered WeChat files are not removed.
This is file-library cleanup, not a workspace reset or secure data erasure. Deleting input
files may prevent old tasks from continuing. Filesystem errors are reported as partial
failures; already-removed copies are not restored. No real user files are deleted by tests.

`状态` is a command; `分析一下学生的学习状态` is ordinary conversation. Numbered commands
require a space before the number. `停止` is not a freeze/resume checkpoint: continuation
inspects existing results and saved context, rather than resuming an interrupted program
at the same instruction. A completed processing turn is not proof of file delivery.
`/status`, `/状态`, `/cancel`, `/取消`, `/help`, `/帮助`, and the main Chinese commands
prefixed with `/` remain available; plain Chinese is the preferred phone input.

Model selection is stored per user and affects requests when they start processing,
including queued requests and continuations. A running request retains its captured model
for both routing and execution. Listing models does not send a chat prompt. Switching
requires a fresh availability check; failures leave the selection unchanged. Model IDs
are account-dependent, may have different capabilities or billing, and availability does
not guarantee identical tool/media behavior. If a selected model disappears, execution
fails explicitly instead of switching to a default. Model numbers are never reassigned
to a different model when the catalog changes.

## Storage and Privacy

Code identifiers, comments, local console messages and documentation use English.
WeChat commands and user-facing replies use Simplified Chinese; their literals and test
fixtures remain Chinese in the source. Command examples below follow the same convention.

`app/data/assistant` contains SQLite message/task/delivery metadata, per-user original
uploads, editable workspaces, separate router/worker Copilot state, and output snapshots.
WeChat raw payloads include reply context references and are sensitive. The entire data
directory is excluded from Git. Do not inspect or publish production runtime logs/state.

The token and WeChat credentials are outside user workspaces. Git ignore and POSIX-style
file modes are not Windows ACL protection. Restrict directory ACLs or move credentials
to a private location for long-term use. Local storage is not encrypted by this application.

Required prompts, file contents, and tool output go to Copilot cloud inference under
the applicable GitHub/provider policies. WeChat messages pass through WeChat services.
Local persistence does not guarantee provider zero retention. Optional cloud sessions
and SDK persistent memory are disabled; no application telemetry exporter is enabled.
This does not disable all provider/runtime service telemetry.

## Permission Boundary

This is a trusted-family prototype with logical directory isolation, **not a sandbox**.
File permission requests and send-file paths are checked against the user workspace.
Known paths in shell requests are checked too, but arbitrary scripts can bypass logical
checks and access anything the host Windows account can access. The token may be present
in runtime memory. Do not treat the arrangement as safe for untrusted users or documents.

Workspace actions are normally approved. The agent is instructed to seek confirmation
for external publication, purchases, messages to others, and global installs. Some
network-writing commands also trigger a programmatic confirmation. These are not a
complete shell/network policy enforcement mechanism. No administrator privileges are granted.

## Reliability and Limits

- Inbox IDs are deduplicated in SQLite; cursors advance after messages are recorded.
- Originals are preserved and copied into editable workspaces; filenames are sanitized.
- Downloads/uploads are limited to 50 MiB per attachment. Inbound decryption checks PKCS7 padding.
- Attachment manifests include the latest 30 records no newer than the initiating text;
  older data remains on disk. Full knowledge indexing and video transcription are not implemented.
- On restart, in-flight work is marked interrupted, never blindly replayed. Ask explicitly
  to continue. Sending interrupted midway is marked uncertain rather than presumed successful.
- Connection establishment failures get bounded retries. Send timeouts are uncertain;
  do not automatically repeat potentially delivered results. Later parts of the same result
  wait for earlier deliveries. Failed/uncertain deliveries currently require operator inspection.
- Session expiry requires another QR login. There is no automatic QR re-login UI yet.
- No scheduled work, desktop automation, automatic retention cleanup, or persistent services
  launched by the agent are included. Maintain local backups and monitor disk usage.

## Local Agent Check

```powershell
uv run --project app wechat-agent local-test --token-file token.txt --data-dir app/data/integration-test --user synthetic --message "Create result.txt containing wechat-agent-ok, verify it, and send that file." --report-file app/data/local-test-report.json
```

This command performs real model/tool work in a synthetic user's workspace but does not
send to WeChat. Its report includes the synthetic task output, unlike the redacted doctor.

## Protocol References

- https://github.com/github/copilot-sdk/tree/main/python
- https://github.com/github/copilot-sdk/blob/main/docs/setup/multi-tenancy.md
- https://docs.cowagent.ai/channels/weixin
- https://github.com/zhayujie/CowAgent/tree/master/channel/weixin

The WeChat adapter implements the observed iLink wire format independently, using httpx
and cryptography. Platform compatibility must be verified with real account traffic.