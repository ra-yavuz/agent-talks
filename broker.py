"""Small, durable, local two-agent channels. No model calls in this module."""
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

CLOSE_QUESTION = "Are you sure the work is done and you want to close the communication channel?"


class CloseConfirmationRequired(ValueError):
    pass


class RoleOccupied(ValueError):
    pass


def default_db():
    return Path(os.environ.get("AGENT_TALKS_DB", "~/.local/share/agent-talks/state.sqlite")).expanduser()


class Broker:
    @staticmethod
    def turn(state):
        return {"ready": "orchestrator", "working": "worker", "review": "orchestrator", "closed": None}[state]

    def __init__(self, path=None):
        path = Path(path or default_db())
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS channels (
                name TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'ready', revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS members (
                channel TEXT NOT NULL, role TEXT NOT NULL, session TEXT UNIQUE NOT NULL,
                token TEXT UNIQUE NOT NULL,
                PRIMARY KEY(channel, role)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, revision INTEGER NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                request_id TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(channel, request_id)
            );
            CREATE TABLE IF NOT EXISTS close_approvals (
                channel TEXT PRIMARY KEY, revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retired_members (
                session TEXT PRIMARY KEY, channel TEXT NOT NULL, role TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS confirm_channel_close
            BEFORE UPDATE OF state ON channels
            WHEN NEW.state='closed' AND OLD.state!='closed'
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM close_approvals WHERE channel=OLD.name AND revision=OLD.revision
                ) THEN RAISE(ABORT, 'Human confirmation required before closing this channel. Reload agent_talks to show the confirmation prompt.') END;
                DELETE FROM close_approvals WHERE channel=OLD.name;
            END;
        """)

    def transaction(self, fn):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = fn()
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def register(self, channel, role, session):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", channel):
            raise ValueError("Channel must be 1–80 letters, digits, dots, dashes or underscores.")
        if role not in ("orchestrator", "worker"):
            raise ValueError("Unknown role.")
        if not session.strip():
            raise ValueError("A stable session ID is required.")

        def run():
            self.db.execute("INSERT OR IGNORE INTO channels(name) VALUES (?)", (channel,))
            old = self.db.execute("SELECT * FROM members WHERE channel=? AND role=?", (channel, role)).fetchone()
            if old:
                if old["session"] != session:
                    raise RoleOccupied("This role belongs to another session. Re-register through the updated tool to confirm replacing it, or use manage.py recover. Do not create a new channel to recover this one.")
                return dict(old)
            if self.db.execute("SELECT state FROM channels WHERE name=?", (channel,)).fetchone()[0] == "closed":
                raise ValueError("This channel is closed. Choose a new channel name.")
            previous = self.db.execute("""SELECT c.state FROM members m
                JOIN channels c ON c.name=m.channel WHERE m.session=?""", (session,)).fetchone()
            if previous:
                if previous["state"] != "closed":
                    raise ValueError("A session can belong to only one active channel and role.")
                self.db.execute("DELETE FROM members WHERE session=?", (session,))
            token = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO members VALUES (?,?,?,?)", (channel, role, session, token))
            self.db.execute("DELETE FROM retired_members WHERE session=?", (session,))
            return dict(channel=channel, role=role, session=session, token=token)
        return self.transaction(run)

    def recovery_plan(self, channel, replacements):
        """Read a token-free snapshot for an explicit session replacement."""
        if not replacements or set(replacements) - {"orchestrator", "worker"}:
            raise ValueError("Choose orchestrator, worker, or both for recovery.")
        if any(not isinstance(s, str) or not s.strip() for s in replacements.values()):
            raise ValueError("A real replacement session ID is required.")
        current = self.db.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
        if not current:
            raise ValueError("Unknown channel.")
        members = {r["role"]: r["session"] for r in self.db.execute(
            "SELECT role,session FROM members WHERE channel=?", (channel,))}
        if set(replacements) - members.keys():
            raise ValueError("An unclaimed role needs ordinary registration, not replacement.")
        future = {**members, **replacements}
        if len(set(future.values())) != len(future):
            raise ValueError("Each role needs a different session.")
        for role, session in replacements.items():
            existing = self.db.execute("SELECT channel,role FROM members WHERE session=?", (session,)).fetchone()
            if existing and (existing["channel"], existing["role"]) != (channel, role):
                raise ValueError("The replacement session already belongs to another channel or role.")
        return {"channel": channel, "state": current["state"], "revision": current["revision"],
                "members": members, "replacements": dict(replacements)}

    def recover(self, plan, *, confirmed=False):
        """Replace sessions only after confirmation of this exact snapshot.

        Rotate tokens, fence old sessions, and invalidate in-flight revisions in
        one transaction. Existing work is never treated as completed or rerun.
        """
        if not confirmed:
            raise ValueError("Human confirmation is required to replace a session.")

        def run():
            current = self.recovery_plan(plan["channel"], plan["replacements"])
            if current != plan:
                raise ValueError("The channel changed during recovery confirmation. Read status and confirm again.")
            channel = plan["channel"]
            changed = {role: session for role, session in plan["replacements"].items()
                       if session != plan["members"][role]}
            for role, session in changed.items():
                self.db.execute("INSERT OR REPLACE INTO retired_members VALUES (?,?,?)",
                                (plan["members"][role], channel, role))
                self.db.execute("UPDATE members SET session=?,token=? WHERE channel=? AND role=?",
                                (session, secrets.token_urlsafe(32), channel, role))
                self.db.execute("DELETE FROM retired_members WHERE session=?", (session,))
            state = plan["state"]
            revision = plan["revision"]
            if changed:
                # Closed channels stay closed; recovery is not a reopen command.
                state = "closed" if state == "closed" else "review"
                revision += 1
                self.db.execute("UPDATE channels SET state=?,revision=? WHERE name=?", (state, revision, channel))
                self.db.execute("DELETE FROM close_approvals WHERE channel=?", (channel,))
                self._system_message(channel, revision, "recovery",
                    "Session recovery replaced: " + ", ".join(sorted(changed)) + ". "
                    "Previous connections for these roles are revoked. History is preserved. "
                    "This does not confirm task completion. Stop any jobs left by the previous sessions. "
                    + ("The channel remains closed." if state == "closed" else
                       "The orchestrator must review the saved history and any unfinished work before sending a new task. The worker must wait."))
            return {"channel": channel, "state": state, "revision": revision,
                    "turn": self.turn(state), "recovered": sorted(changed)}
        return self.transaction(run)

    def receive(self, token, after_revision):
        member = self.member(token)
        # Reads do not erase messages: reconnecting clients can replay safely.
        rows = self.db.execute("""SELECT * FROM messages WHERE channel=? AND recipient=?
            AND revision>? ORDER BY revision""", (member["channel"], member["session"], after_revision)).fetchall()
        return [dict(row) for row in rows]

    def member(self, token):
        row = self.db.execute("SELECT * FROM members WHERE token=?", (token,)).fetchone()
        if not row:
            raise ValueError("Invalid or replaced member token. Register again with your channel, role and current session ID. Do not reuse a replaced session's token.")
        return row

    def status(self, token):
        member = self.member(token)
        channel = dict(self.db.execute("SELECT * FROM channels WHERE name=?", (member["channel"],)).fetchone())
        channel["role"] = member["role"]
        channel["turn"] = self.turn(channel["state"])
        channel["your_turn"] = channel["turn"] == member["role"]
        channel["members"] = [dict(r) for r in self.db.execute(
            "SELECT role,session FROM members WHERE channel=?", (member["channel"],))]
        channel["messages"] = [dict(r) for r in self.db.execute(
            "SELECT * FROM messages WHERE channel=? ORDER BY revision DESC LIMIT 20", (member["channel"],))]
        return channel

    def prepare_wait(self, token, after_revision):
        """Never let a consumed task and two waits strand the channel.

        A worker that explicitly waits past its current task yields, without
        claiming completion. Return a compatible cursor for old MCP processes.
        """
        def run():
            member = self.member(token)
            current = self.db.execute("SELECT * FROM channels WHERE name=?", (member["channel"],)).fetchone()
            if after_revision < 0 or after_revision > current["revision"]:
                raise ValueError("Invalid wait revision. Read status.")
            if member["role"] == "worker" and current["state"] == "working" and after_revision == current["revision"]:
                peer = self.db.execute("SELECT session FROM members WHERE channel=? AND role='orchestrator'", (member["channel"],)).fetchone()
                if not peer:
                    raise ValueError("The orchestrator is missing. No work is authorized.")
                ident = str(uuid.uuid4())
                self.db.execute("UPDATE channels SET state='review',revision=revision+1 WHERE name=?", (member["channel"],))
                self.db.execute("""INSERT INTO messages
                    (id,channel,revision,sender,recipient,kind,text,request_id,created)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (ident, member["channel"], current["revision"] + 1,
                    "system", peer["session"], "recovery",
                    "The worker entered wait after receiving its current task, without sending a report. "
                    "Control has returned to the orchestrator to prevent both agents waiting. "
                    "This is not a completion report. Read status, then request the missing result, "
                    "send a revised task, or close the channel. Do not wait again while it is your turn.",
                    ident, time.time()))
            # The old server only wakes for unread messages. Replay the report
            # that already gave this orchestrator its turn, even with a used cursor.
            if member["role"] == "orchestrator" and current["state"] == "review":
                return min(after_revision, current["revision"] - 1)
            return after_revision
        return self.transaction(run)

    def send(self, token, kind, text, revision, request_id, *, close_confirmed=False):
        if not text.strip() or len(text) > 50000 or not request_id or len(request_id) > 120:
            raise ValueError("Provide a nonempty message (up to 50,000 characters) and request ID (up to 120).")
        transitions = {
            ("orchestrator", "task", "ready"): "working",
            ("orchestrator", "task", "review"): "working",
            ("orchestrator", "close", "ready"): "closed",
            ("orchestrator", "close", "review"): "closed",
            ("worker", "report", "working"): "review",
        }

        def run():
            member = self.member(token)
            channel = member["channel"]
            old = self.db.execute("SELECT * FROM messages WHERE channel=? AND request_id=?", (channel, request_id)).fetchone()
            if old:
                if (old["sender"], old["kind"], old["text"], old["revision"] - 1) != (member["role"], kind, text, revision):
                    raise ValueError("Request ID was already used for a different message.")
                if kind == "close" and self.db.execute("SELECT state FROM channels WHERE name=?", (channel,)).fetchone()[0] != "closed":
                    raise ValueError("The channel was reopened. A new close needs a new request ID and new human confirmation.")
                return dict(old)
            current = self.db.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
            if current["revision"] != revision:
                raise ValueError("Stale revision. Read status before replying.")
            state = transitions.get((member["role"], kind, current["state"]))
            if not state:
                raise ValueError(f"{member['role']} cannot send {kind} while channel is {current['state']}.")
            recipient = "worker" if member["role"] == "orchestrator" else "orchestrator"
            peer = self.db.execute("SELECT session FROM members WHERE channel=? AND role=?", (channel, recipient)).fetchone()
            if not peer:
                raise ValueError("Waiting for the other role to register.")
            if kind == "close":
                if not close_confirmed:
                    raise CloseConfirmationRequired(CLOSE_QUESTION)
                self.db.execute("INSERT OR REPLACE INTO close_approvals VALUES (?,?)", (channel, revision))
            ident = str(uuid.uuid4())
            self.db.execute("UPDATE channels SET state=?,revision=revision+1 WHERE name=?", (state, channel))
            self.db.execute("""INSERT INTO messages
                (id,channel,revision,sender,recipient,kind,text,request_id,created)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (ident, channel, revision + 1, member["role"], peer["session"], kind, text, request_id, time.time()))
            return dict(self.db.execute("SELECT * FROM messages WHERE id=?", (ident,)).fetchone())
        return self.transaction(run)

    def human_close(self, channel, revision):
        """Called only after a human confirms the terminal prompt."""
        def run():
            current = self.db.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
            if not current:
                raise ValueError("Unknown channel.")
            if current["revision"] != revision:
                raise ValueError("The channel changed while confirmation was pending. Review and confirm again.")
            if current["state"] == "closed":
                return
            self.db.execute("INSERT OR REPLACE INTO close_approvals VALUES (?,?)", (channel, revision))
            self.db.execute("UPDATE channels SET state='closed',revision=revision+1 WHERE name=?", (channel,))
            self._system_message(channel, revision + 1, "close", "The human operator confirmed closing the channel.")
        return self.transaction(run)

    def reopen(self, channel):
        def run():
            current = self.db.execute("SELECT * FROM channels WHERE name=?", (channel,)).fetchone()
            if not current:
                raise ValueError("Unknown channel.")
            if current["state"] != "closed":
                return {"channel": channel, "state": current["state"], "revision": current["revision"], "turn": self.turn(current["state"]), "reopened": False}
            members = self.db.execute("SELECT role FROM members WHERE channel=?", (channel,)).fetchall()
            if {m["role"] for m in members} != {"orchestrator", "worker"}:
                raise ValueError("Both original members must still belong to this channel. A member has joined another channel.")
            self.db.execute("UPDATE channels SET state='review',revision=revision+1 WHERE name=?", (channel,))
            self._system_message(channel, current["revision"] + 1, "reopen",
                "The channel was reopened. It is the orchestrator's turn to review the remaining work and send the next task. The worker must wait for that task.")
            return {"channel": channel, "state": "review", "revision": current["revision"] + 1, "turn": "orchestrator", "reopened": True}
        return self.transaction(run)

    def _system_message(self, channel, revision, kind, text):
        # Caller holds the write transaction. Notify each original member.
        for member in self.db.execute("SELECT session FROM members WHERE channel=?", (channel,)).fetchall():
            ident = str(uuid.uuid4())
            self.db.execute("""INSERT INTO messages
                (id,channel,revision,sender,recipient,kind,text,request_id,created)
                VALUES (?,?,?,?,?,?,?,?,?)""", (ident, channel, revision, "system", member["session"], kind, text, ident, time.time()))

    def guard(self, session, tool_name):
        """Host hook guard, not an OS sandbox or a semantic task checker."""
        retired = self.db.execute("SELECT channel FROM retired_members WHERE session=?", (session,)).fetchone()
        if retired:
            return (f"Agent Talks: this session was replaced in channel {retired['channel']}. "
                    "Stop this session and its background jobs. Work must continue in the replacement session.")
        row = self.db.execute("""SELECT c.state,m.role FROM members m
            JOIN channels c ON c.name=m.channel WHERE m.session=?""", (session,)).fetchone()
        if not row or row["state"] == "closed" or row["role"] == self.turn(row["state"]):
            return None
        if tool_name in {"mcp__agent_talks__register", "mcp__agent_talks__status", "mcp__agent_talks__wait", "mcp__agent_talks__send"}:
            return None
        return f"Agent Talks: it is the {self.turn(row['state'])}'s turn. Call agent_talks wait; do no work until control returns."
