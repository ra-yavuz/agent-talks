"""MCP entry point. Each agent runs a process; all share one SQLite file."""
import argparse
import asyncio
import sys
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from broker import Broker, CLOSE_QUESTION, CloseConfirmationRequired, RoleOccupied


INSTRUCTIONS = """Agent Talks links an orchestrator and a worker in a named local channel.
Register only when the user asks. Use your real session ID, never the folder name.
Keep the returned token private and reuse it. A session can join one channel only.
If you lose the token or reconnect, call register again with the same channel,
role and your current session ID. This returns your existing membership and status.
If your session ID changed, registration offers human-confirmed replacement of
the old session. Do not claim the channel cannot be rejoined or create another
channel to bypass a registration error. Show the actual error and recovery path.
After registration read status. Only the orchestrator assigns tasks or closes a channel.
Only the worker reports completion or a blocker. A report transfers control to the
orchestrator. Do no more work until a new task arrives. Finish or stop all background
jobs before reporting. Never submit work tools and a report concurrently.
After a successful send, print its exact text in normal chat commentary, prefixed with
its channel and recipient, unless a hook already displayed it. On error, show that it failed.
Use the current revision and a new request_id for each message. On retry use exactly
the same request_id, revision and text. After sending, call wait after that message's
revision. Do not send status chatter back and forth. Read status after reconnecting;
act only on the latest state. The channel has one owner: ready/review means
orchestrator, working means worker. When it is the peer's turn, keep a wait call open.
When the orchestrator owns the turn, review or discuss the result with the user;
do not wait for another worker message. You may finish a reply to the user while
keeping the channel open. A worker waiting past its current task yields control
back to the orchestrator; this never claims that the task completed.
If wait times out without a message, wait again without doing work. A timeout,
disconnect or missing peer never authorizes work. The user can interrupt or stop this
workflow. Closing always requires the user's confirmation through the tool prompt.
If confirmation is declined or unavailable, leave the channel open and do not ask
repeatedly. On confirmed close, print the conclusion and end the turn. Reopen a
closed channel only when the user asks; the orchestrator gets the next turn.
This protocol enforces message order. Host hooks are needed to block other tools.
It does not sandbox an agent or judge whether actions fit the meaning of a task.
"""

mcp = MCPServer("agent_talks", instructions=INSTRUCTIONS)


class CloseAnswer(BaseModel):
    confirm: bool = Field(default=False, description="Yes, the work is done and I want to close this channel.")


class RecoveryAnswer(BaseModel):
    confirm: bool = Field(default=False, description="Replace the previous session for this role and revoke its access to the channel.")


@mcp.tool(structured_output=True)
async def register(channel: str, role: Literal["orchestrator", "worker"], session_id: str, ctx: Context) -> dict[str, Any]:
    """Join a named channel. Use CODEX_THREAD_ID for Codex; the host's session ID elsewhere.

    Rejoin after a disconnect or lost token using the same arguments. A changed
    session ID requires human confirmation to replace the old session. History is
    preserved. Read the returned status before working or waiting.
    """
    broker = Broker()
    try:
        try:
            result = broker.register(channel, role, session_id)
        except RoleOccupied:
            plan = broker.recovery_plan(channel, {role: session_id})
            question = (f"Channel: {channel}\nRole: {role}\n"
                        f"Previous session: {plan['members'][role]}\nNew session: {session_id}\n\n"
                        "Replace the previous session for this role? Its channel access will be revoked. "
                        "History will be kept. Open channels return to the orchestrator for review. "
                        "Stop any jobs still running in the previous session before continuing.")
            try:
                answer = await ctx.elicit(question, RecoveryAnswer)
            except Exception:
                return {"status": "recovery_confirmation_required", "channel": channel, "role": role,
                        "instruction": "This channel can be recovered. Human confirmation is unavailable in this connection. "
                        "Use manage.py recover CHANNEL --ROLE NEW_SESSION_ID in a human terminal, "
                        "then call register again. No membership has changed."}
            if answer.action != "accept" or not answer.data.confirm:
                return {"status": "recovery_cancelled", "instruction": "Membership is unchanged. Do not keep asking for confirmation."}
            broker.recover(plan, confirmed=True)
            result = broker.register(channel, role, session_id)
        result["instructions"] = INSTRUCTIONS
        result["status"] = broker.status(result["token"])
        return result
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        broker.db.close()


@mcp.tool(structured_output=True)
def status(token: str) -> dict[str, Any]:
    """Read your channel state, current revision, members and last 20 messages."""
    broker = Broker()
    try:
        return broker.status(token)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        broker.db.close()


@mcp.tool(structured_output=True)
async def send(token: str, kind: Literal["task", "report", "close"], text: str,
               revision: int, request_id: str, ctx: Context) -> dict[str, Any]:
    """Persist a message and transfer control atomically. Display the sent text in chat.

    Tasks/close are orchestrator-only. Reports are worker-only. A report can describe
    completion, a blocker or a question. No work after a report until the next task.
    Retry a failed transport call with exactly the same arguments to avoid duplicates.
    After a task or report, wait with after_revision equal to the returned revision.
    A close requests human confirmation; denial leaves the channel open for discussion.
    """
    broker = Broker()
    try:
        try:
            return broker.send(token, kind, text, revision, request_id)
        except CloseConfirmationRequired:
            channel = broker.member(token)["channel"]
            try:
                answer = await ctx.elicit(f"Channel: {channel}\n\n{CLOSE_QUESTION}\n\nProposed conclusion:\n{text}", CloseAnswer)
            except Exception:
                return {"status": "confirmation_required", "question": CLOSE_QUESTION,
                        "instruction": "The channel remains open. User confirmation could not be obtained. Use the human terminal close command or reconnect agent_talks to enable its confirmation prompt."}
            if answer.action != "accept" or not answer.data.confirm:
                return {"status": "close_cancelled", "instruction": "The user did not confirm closing. The channel remains open. Continue the discussion; do not keep asking to close."}
            return broker.send(token, kind, text, revision, request_id, close_confirmed=True)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        broker.db.close()


@mcp.tool(structured_output=True)
def reopen(token: str) -> dict[str, Any]:
    """Reopen your closed channel only when the user asks. Preserve members/history; orchestrator owns the next turn."""
    broker = Broker()
    try:
        return broker.reopen(broker.member(token)["channel"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        broker.db.close()


async def wait_for_message(token, after_revision, timeout_seconds, path=None):
    if after_revision < 0 or not 1 <= timeout_seconds <= 3600:
        raise ValueError("Use a nonnegative revision and timeout between 1 and 3600 seconds.")
    broker = Broker(path)
    try:
        after_revision = broker.prepare_wait(token, after_revision)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            messages = broker.receive(token, after_revision)
            state = broker.status(token)
            if after_revision > state["revision"]:
                raise ValueError("Revision is ahead of the channel. Read status.")
            turn = {"turn": state["turn"], "your_turn": state["your_turn"]}
            if state["your_turn"] and state["role"] == "orchestrator" and len(state["members"]) == 2:
                return {"messages": messages, "state": state["state"], "revision": state["revision"], **turn,
                        "instruction": "It is your turn as orchestrator. Read status, then send a task or close the channel. Do not wait again before deciding."}
            if messages or state["state"] == "closed":
                return {"messages": messages, "state": state["state"], "revision": state["revision"], **turn}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return {"messages": [], "state": state["state"], "revision": state["revision"], **turn,
                        "instruction": "No new message. Wait again. No work is authorized by a timeout."}
            await asyncio.sleep(min(0.25, remaining))
    finally:
        broker.db.close()


@mcp.tool(structured_output=True)
async def wait(token: str, after_revision: int, timeout_seconds: int = 3600) -> dict[str, Any]:
    """Idle without model requests until a peer message arrives. Keep this call open.

    after_revision is the last revision you already handled or sent. Set the host's
    tool timeout above timeout_seconds. If canceled, reconnect using status and wait;
    messages remain stored. This cannot wake a chat that has ended or been closed.
    """
    try:
        return await wait_for_message(token, after_revision, timeout_seconds)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    argparse.ArgumentParser(description="Local two-agent MCP server over standard input/output.", epilog='Provided as is, without warranty. You accept all risk from installation and use.').parse_args()
    print('Provided as is, without warranty. You accept all risk from installation and use.', file=sys.stderr)
    mcp.run(transport="stdio")
