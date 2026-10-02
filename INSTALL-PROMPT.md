# Install Agent Talks for the current user

Use these instructions only when the user asks to install Agent Talks. Read the
repository's README.md and SECURITY.md first. Do not create channels or start work
merely because installation was requested.

1. Check the operating system, Python version, `venv` support, and Codex installation.
   The supported setup is Linux, Python 3.11+, and a Codex CLI with MCP and command
   hooks. Do not claim another host is supported without checking its integration.
2. Clone https://github.com/ra-yavuz/agent-talks into a permanent user-owned directory
   chosen from the user's preferences. If it already exists, inspect its remote and
   working changes before updating it. Do not overwrite a different project.
3. Inspect `requirements.txt`, `install.py`, `hook.py`, and `server.py`. Explain that
   this installs a local MCP server and three workflow hooks, stores plaintext
   messages outside the checkout, and requires the user's hook review.
4. Create `.venv` using a compatible Python interpreter. Install requirements in
   that environment. Do not use sudo, change system Python, modify global approval
   settings, or disable hook trust. If an OS prerequisite is missing, report the
   exact missing prerequisite before making system-wide changes.
5. Run `.venv/bin/python -m unittest -v`. These tests use temporary databases and
   no model calls. Stop and report failures; do not weaken or bypass tests.
6. Run `.venv/bin/python install.py`. Honor the intended `CODEX_HOME`, or pass
   `--home` explicitly if the user selected a different configuration directory.
   The installer preserves unrelated settings and backs up changed files. If it
   finds a conflicting Agent Talks entry, inspect it and explain the conflict;
   do not erase the config or silently replace another installation.
7. Tell the user to open `/hooks` and review and trust only the three hook entries
   that run this checkout's `hook.py`. This is an interactive host requirement.
   Do not mark hooks trusted by editing internal trust stores.
8. Tell the user to start or resume both chats and check `/mcp` for `agent_talks`.
   Do not terminate ongoing chats or jobs. If an existing connection still uses
   old code, explain that a fresh connection is needed.
9. Report the installation path, test result, configuration files changed, and any
   remaining manual step. Give the two registration prompts from README.md with
   the user's chosen channel name. If no name or objective was given, do not invent
   an active task or start a channel. Both sessions must be on the same machine
   and use the same user account and database.

Keep registration tokens, database files, config backups, conversation history, and
provider credentials out of Git and out of the installation report. Do not install
an optional messaging service or create external chat rooms as part of this setup.

Provided as is, without warranty. The user accepts all risk from installation and use.
