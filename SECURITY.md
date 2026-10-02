# Security and privacy

Agent Talks coordinates cooperating local agents. It is not a sandbox, an access
control boundary between untrusted agents, or a review of what a task authorizes.
The supported integration guards covered Codex tool calls through trusted hooks.
Disabled hooks, exempt tool paths, direct filesystem access, and existing background
jobs are outside that guard. Do not give an agent permissions you would not otherwise
allow it to use.

## Stored information

The default SQLite database is in `~/.local/share/agent-talks/`, outside the source
checkout. It stores member tokens, session identifiers, and plaintext tasks and
reports. The code creates the database with mode 0600 and its parent with mode 0700
when creating it. Those modes do not protect against the same OS user, administrators,
malware with that user's access, or copies in backups. Existing parent directory
permissions and backup access remain the operator's responsibility.

No credential, server address, or live channel record is supplied by this repository.
No telemetry is implemented. Dependencies are downloaded during setup, and your
coding agent may send message contents to its model provider. Do not place passwords,
keys, customer records, or confidential material into messages without considering
that data flow. Keep databases, credentials, logs, and local configuration out of Git.

## Recovery and confirmation

A same-session reconnect keeps its token and turn. Replacing a session requires
human confirmation, rotates the token, and invalidates stale revisions. Host hooks
block the retired session's subsequent covered tool calls. Already-running commands
are not stopped. End old jobs before continuing work. This protection assumes the
local broker, hooks, host, and OS user are trusted.

Close and replacement confirmation use the host's MCP confirmation UI, or an explicit
terminal prompt. An unavailable UI is not approval. The terminal controls are intended
for the human operator; an unrestricted agent with shell access is not isolated from
them. Turn rules do not make an unrestricted agent incapable of changing local state.

## Reporting a problem

Do not publish tokens, database files, real task messages, or private paths in a
public issue. Use a minimal reproduction with temporary data. For sensitive reports,
use GitHub's private vulnerability reporting on this repository when available.
Otherwise open an issue asking for a private reporting route without disclosure.

This software is provided as is, without warranty. You accept all risk from its use.
See LICENSE for the full terms.
