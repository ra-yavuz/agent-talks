# Agent Talks

> Let an orchestrator and a worker exchange tasks directly, with one clear turn owner.

Agent Talks connects two open coding-agent sessions on the same machine. The
orchestrator assigns work, the worker reports a result or blocker, and control
returns to the orchestrator for review. Named channels keep multiple pairs separate.
You can follow their messages in the normal agent console instead of copying
responses between chats.

A personal open-source project by **Ramazan Yavuz**. Independent of OpenAI and
other agent vendors. [Project page](https://ra-yavuz.github.io/agent-talks/) ·
[How it works](https://ramazan-yavuz.tr/articles/agent-talks-giving-two-agents-a-turn.html)

## Install

**Supported setup:** Linux, Python 3.11 or newer with `venv`, and two Codex CLI
sessions under the same operating-system user. Codex must support MCP tools and
`PreToolUse`, `PostToolUse`, and `Stop` hooks. MCP means Model Context Protocol.
Other MCP hosts may use the broker, but their tool guards and confirmation UI
need separate integration. Native Windows and macOS are not verified.

Hand this to your coding agent:

> Install Agent Talks from https://github.com/ra-yavuz/agent-talks. Read README.md,
> SECURITY.md, and INSTALL-PROMPT.md, then follow INSTALL-PROMPT.md. Preserve my
> existing settings and stop before any step that needs my interactive hook review.

Or install from a terminal in a permanent location:

```bash
git clone https://github.com/ra-yavuz/agent-talks.git
cd agent-talks
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v
.venv/bin/python install.py
```

The installer records this checkout's absolute paths. Keep the directory in place.
It adds `agent_talks` to `~/.codex/config.toml`, sets a 3,700-second tool timeout,
and merges three hooks into `~/.codex/hooks.json`. It backs up changed files and
preserves unrelated settings. `CODEX_HOME` or `install.py --home PATH` selects a
different Codex configuration directory. Installation does not register channels.

Open `/hooks` in Codex and review and trust the three entries that run this
checkout's `hook.py`. Start or resume both chats to load the MCP server; check
`/mcp` for `agent_talks`. Without trusted hooks, the broker still enforces message
order, but other tool calls are not guarded and the console hook is inactive.
See [Codex hook documentation](https://learn.chatgpt.com/docs/hooks).

No Debian package, background daemon, listening network port, or separate model
API key is needed for this core tool. Your agent provider's usual costs still apply.

## Start a pair

Give the orchestrator your objective first. Both sessions must use the same machine,
user account, database, and channel name. For example:

**Orchestrator**

> Register in agent_talks channel example-work as orchestrator, using your current
> CODEX_THREAD_ID. Use our current objective. Once the worker joins, send the first
> task. Follow the tool's turn rules and review each worker report before continuing.

**Worker**

> Register in agent_talks channel example-work as worker, using your current
> CODEX_THREAD_ID. Wait for the orchestrator's first task. Complete only assigned
> work, report the result or blocker, and wait for the next instruction.

Use a different channel name for each pair. A session belongs to one active pair;
each role has one owner. Registration alone does not define or authorize a task.
Keep both chats open with a waiting tool call when it is the other agent's turn.
An ended chat cannot be woken by this tool.

## Turn order

```text
ready -> orchestrator sends task -> working
working -> worker sends report -> review
review -> orchestrator sends next task -> working
ready/review -> human-confirmed close -> closed
```

`ready` and `review` belong to the orchestrator; `working` belongs to the worker.
Status responses include `turn` and `your_turn`. During review the orchestrator
can discuss the result with you and finish a chat reply without closing the channel.

| Tool | Purpose |
| --- | --- |
| `register(channel, role, session_id)` | Join or recover membership and return current status. |
| `status(token)` | Read state, revision, members, and the last 20 messages. |
| `send(token, kind, text, revision, request_id)` | Store a task, report, or confirmed close and transfer control. |
| `wait(token, after_revision, timeout_seconds=3600)` | Hold the call until a message, turn change, close, or timeout. |
| `reopen(token)` | Reopen a closed channel when the user asks; orchestrator goes next. |

Waiting checks local SQLite state every 250 milliseconds without model requests.
The surrounding agent may use tokens when the call returns or times out. A timeout
never grants permission to work. If the worker waits past its received task without
a report, control returns to the orchestrator with a recovery notice. That notice
does not claim the task completed.

Messages are stored before a send returns. A revision rejects stale changes;
retrying the same request ID with identical arguments returns the original result.
Delivery to the database does not prove the peer read or acted on a message.
Successful sends appear in the sender's console through the hook, or through the
agent's instructed echo. The receiver reads them through `wait` or `status`.

## Recover, close, and reopen

Lost token or disconnected tool: call `register` again with the original channel,
role, and current session ID. The same session gets its existing token and turn back.
A replacement session ID asks for human confirmation before taking over. Recovery
preserves history, rotates the token, guards the replaced session, and returns an
open channel to the orchestrator for review. It leaves a closed channel closed.

To replace either or both roles from a human terminal, run this from the checkout:

```bash
.venv/bin/python manage.py recover example-work --orchestrator NEW_ORCHESTRATOR_SESSION_ID --worker NEW_WORKER_SESSION_ID
```

Use actual new session IDs; omit a role that is staying in place. The command shows
the proposed change and asks for confirmation. Then call `register` in the replacement
sessions. A change during confirmation is rejected. Stop jobs still running in old
sessions before assigning more work; recovery cannot kill existing commands.

Closing always requires human confirmation. Declining or lacking a confirmation UI
leaves the channel open. Terminal controls are also available:

```bash
.venv/bin/python manage.py list
.venv/bin/python manage.py close example-work
.venv/bin/python manage.py reopen example-work
```

Closing preserves history and releases the active members' tool guards. Reopening
preserves membership and puts the orchestrator in charge. It fails if an original
member has moved to another channel. Read status after any recovery or reconnect.
Changes to server code require a fresh MCP connection; hook code is loaded on each
hook invocation.

## Data and boundaries

State lives in `~/.local/share/agent-talks/state.sqlite`, outside the checkout.
It includes member tokens, session IDs, and **plaintext message history**. Set
`AGENT_TALKS_DB` consistently for tools, hooks, and terminal commands to change the
location. Do not sync the live database between machines or commit it to Git.
There is no telemetry or built-in remote service. The messages still enter your
agent's context and may be processed by its provider.

The broker checks roles, revisions, and turn order. Trusted hooks guard covered
Codex tools while a session does not own the turn. They do not sandbox the agents,
verify the quality of work, enforce the meaning of a task, or stop commands already
running. Agents sharing an operating-system user can access the same files and are
not isolated from each other. See [SECURITY.md](SECURITY.md).

This public release contains the standalone core. It does not include a Matrix
bridge, hosting configuration, credentials, channel records, or sample conversations
from a running installation.

## Develop and remove

Run `.venv/bin/python -m unittest -v` from the checkout. Tests use temporary databases
and real MCP subprocesses without contacting models. They cover turn handoffs,
concurrency, retries, session replacement, and confirmation. Passing them does not
prove live console rendering or hook trust in every host version.

To uninstall, close active channels first, remove only the `mcp_servers.agent_talks`
entry from your Codex config and the three hook entries pointing to this checkout,
then reconnect Codex. Preserve unrelated settings. Removing the checkout does not
delete saved channel history; keep or delete that data separately as you choose.

## License and disclaimer

[MIT](LICENSE). Copyright 2026 Ramazan Yavuz.

Provided **as is, without warranty of any kind**. You accept all risk from installing
or using this software, including unintended agent actions, data loss, disclosure,
and provider costs. It is experimental workflow tooling, not a security boundary or
a guarantee of correct work. Review agent permissions, protect secrets, and keep
backups. The MIT license contains the applicable warranty and liability terms.
