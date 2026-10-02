"""Optional Codex guard: block covered worker tools outside an assignment."""
import json
import sys

from broker import Broker, CLOSE_QUESTION, default_db


def handle(data, path=None):
    if path is None and not default_db().exists() and data.get("tool_name") != "mcp__agent_talks__register":
        return {}
    broker = Broker(path)
    try:
        session = data.get("session_id", "")
        event = data.get("hook_event_name")
        name = data.get("tool_name", "")
        args = data.get("tool_input", {})
        reason = None
        member = broker.db.execute("SELECT * FROM members WHERE session=?", (session,)).fetchone()
        if event == "PreToolUse" and broker.db.execute(
                "SELECT 1 FROM retired_members WHERE session=?", (session,)).fetchone():
            return {"hookSpecificOutput": {"hookEventName": event,
                    "permissionDecision": "deny", "permissionDecisionReason": broker.guard(session, name)}}
        if event == "PreToolUse" and name == "mcp__agent_talks__register":
            if args.get("session_id") != session:
                reason = "Register with the current CODEX_THREAD_ID only."
        elif event == "PreToolUse" and name in {"mcp__agent_talks__status", "mcp__agent_talks__send", "mcp__agent_talks__wait", "mcp__agent_talks__reopen"}:
            try:
                if broker.member(args.get("token", ""))["session"] != session:
                    reason = "This Agent Talks token belongs to another session."
            except ValueError:
                reason = "Invalid or replaced Agent Talks token. "
                if member:
                    reason += (f"Recover it by calling agent_talks register with channel={member['channel']}, "
                               f"role={member['role']}, session_id={session}. This channel can be rejoined.")
                else:
                    reason += "Call register with the original channel, role and your current CODEX_THREAD_ID. A replacement session requires human confirmation."
        if reason:
            return {"hookSpecificOutput": {"hookEventName": event,
                    "permissionDecision": "deny", "permissionDecisionReason": reason}}
        if not member:
            return {}
        if event == "PostToolUse" and name == "mcp__agent_talks__send":
            response = data.get("tool_response", {})
            if isinstance(response, str):
                response = json.loads(response)
            message = response.get("structuredContent")
            if not response.get("isError") and isinstance(message, dict) and message.get("channel") == member["channel"]:
                peer = "worker" if message["sender"] == "orchestrator" else "orchestrator"
                return {"systemMessage": f"Agent Talks [{message['channel']}] {message['sender']} → {peer}:\n{message['text']}",
                        "hookSpecificOutput": {"hookEventName": event,
                        "additionalContext": "The sent message was displayed in the normal console. Do not echo it again. Call wait next unless the channel closed."}}
        if event == "PreToolUse":
            if name == "mcp__agent_talks__send" and args.get("kind") == "close":
                return {"systemMessage": CLOSE_QUESTION,
                        "hookSpecificOutput": {"hookEventName": event,
                        "additionalContext": "Channel closure requires explicit human confirmation. The MCP tool requests it. If it reports that confirmation is unavailable, leave the channel open and tell the user to reconnect agent_talks. Never treat silence or your own judgment as confirmation."}}
            if name == "mcp__agent_talks__wait":
                # Hooks are fresh processes. This also repairs calls made by MCP
                # servers that were started before the broker fix was installed.
                cursor = broker.prepare_wait(member["token"], args["after_revision"])
                state = broker.status(member["token"])
                output = {"hookEventName": event,
                          "additionalContext": f"Agent Talks turn: {state['turn'] or 'closed'}. The channel state is shared; it does not mean both agents should work."}
                if state["role"] == "orchestrator" and state["your_turn"]:
                    output["additionalContext"] += " Review or discuss the result with the user. Do not call wait again while you own the turn. You may end your reply while leaving the channel open."
                if cursor != args["after_revision"]:
                    output.update(permissionDecision="allow", updatedInput={**args, "after_revision": cursor})
                return {"hookSpecificOutput": output}
            reason = broker.guard(session, data.get("tool_name", ""))
            if reason:
                return {"hookSpecificOutput": {"hookEventName": event,
                        "permissionDecision": "deny", "permissionDecisionReason": reason}}
        if event == "Stop":
            state = broker.status(member["token"])
            # A review may be a conversation with the user. Ending that reply
            # does not yield the turn and must never force another peer wait.
            if state["role"] == "orchestrator" and state["your_turn"]:
                return {}
            if state["state"] != "closed":
                reason = "Agent Talks is active. "
                if state["role"] == "worker" and state["state"] == "working":
                    reason += "Report the task result or blocker using agent_talks send, then call wait."
                else:
                    reason += "Use agent_talks status, handle any message, then keep agent_talks wait open."
                return {"decision": "block", "reason": reason}
        return {}
    finally:
        broker.db.close()


if __name__ == "__main__":
    try:
        print(json.dumps(handle(json.load(sys.stdin))))
    except Exception as exc:
        print(f"Agent Talks guard failed: {exc}", file=sys.stderr)
        sys.exit(2)
