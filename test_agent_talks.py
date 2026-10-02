import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from mcp.server.mcpserver.exceptions import ToolError

from broker import Broker, CloseConfirmationRequired
from hook import handle
from server import wait_for_message
from server import send as send_tool


class Channels(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.b = Broker(self.path)
        self.o = self.b.register("123", "orchestrator", "o")["token"]
        self.w = self.b.register("123", "worker", "w")["token"]

    def tearDown(self):
        self.b.db.close()
        self.temp.cleanup()

    def task(self):
        return self.b.send(self.o, "task", "Do task one", 0, "task-1")

    def test_cycle_and_close(self):
        self.task()
        self.assertEqual(self.b.status(self.w)["state"], "working")
        self.b.send(self.w, "report", "Done", 1, "report-1")
        self.assertEqual(self.b.status(self.w)["state"], "review")
        self.b.send(self.o, "task", "Fix one item", 2, "task-2")
        self.b.send(self.w, "report", "Fixed", 3, "report-2")
        self.b.send(self.o, "close", "Accepted", 4, "close", close_confirmed=True)
        self.assertEqual(self.b.status(self.w)["state"], "closed")

    def test_illegal_transitions(self):
        for token, kind in [(self.w, "task"), (self.w, "report"), (self.w, "close")]:
            with self.assertRaises(ValueError):
                self.b.send(token, kind, "invalid", 0, "invalid")
        self.task()
        for kind in ["task", "close", "report"]:
            with self.assertRaises(ValueError):
                self.b.send(self.o, kind, "invalid", 1, "invalid")

    def test_retry_is_idempotent_and_conflicts_fail(self):
        first = self.task()
        self.assertEqual(first, self.task())
        self.b.send(self.w, "report", "Done", 1, "report")
        self.assertEqual(first, self.task())
        self.assertEqual(self.b.status(self.o)["revision"], 2)
        with self.assertRaises(ValueError):
            self.b.send(self.o, "task", "Different", 0, "task-1")

    def test_stale_reply(self):
        self.task()
        with self.assertRaises(ValueError):
            self.b.send(self.w, "report", "Done", 0, "report")

    def test_role_and_session_ownership(self):
        self.assertEqual(self.o, self.b.register("123", "orchestrator", "o")["token"])
        for args in [("123", "worker", "someone"), ("456", "worker", "o")]:
            with self.assertRaises(ValueError):
                self.b.register(*args)
        with self.assertRaises(ValueError):
            self.b.status("bad-token")

    def test_parallel_channels_do_not_mix(self):
        o2 = self.b.register("456", "orchestrator", "o2")["token"]
        w2 = self.b.register("456", "worker", "w2")["token"]
        self.task()
        self.assertEqual(self.b.receive(w2, 0), [])
        self.b.send(o2, "task", "Other task", 0, "task-1")
        self.assertEqual(self.b.receive(self.w, 0)[0]["text"], "Do task one")
        self.assertEqual(self.b.receive(w2, 0)[0]["text"], "Other task")

    def test_reconnect_preserves_messages(self):
        self.task()
        second = Broker(self.path)
        try:
            self.assertEqual(second.receive(self.w, 0), self.b.receive(self.w, 0))
            self.assertEqual(second.receive(self.w, 1), [])
        finally:
            second.db.close()

    def test_missing_peer_does_not_authorize_work(self):
        orphan = self.b.register("orphan", "orchestrator", "orphan")["token"]
        with self.assertRaises(ValueError):
            self.b.send(orphan, "task", "Do it", 0, "one")
        self.assertEqual(self.b.status(orphan)["state"], "ready")

    def test_concurrent_sends_only_one_wins(self):
        def submit(number):
            broker = Broker(self.path)
            try:
                return broker.send(self.o, "task", str(number), 0, str(number))
            except ValueError:
                return None
            finally:
                broker.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, [1, 2]))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.b.status(self.o)["revision"], 1)

    def test_guard_and_stop(self):
        tool = {"session_id": "w", "hook_event_name": "PreToolUse", "tool_name": "Bash"}
        self.assertEqual(handle(tool, self.path)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.task()
        self.assertEqual(handle(tool, self.path), {})
        self.assertEqual(handle({"session_id": "w", "hook_event_name": "Stop"}, self.path)["decision"], "block")
        self.b.send(self.w, "report", "Done", 1, "report")
        self.assertEqual(handle(tool, self.path)["hookSpecificOutput"]["permissionDecision"], "deny")
        for name in ["mcp__agent_talks__status", "mcp__agent_talks__wait"]:
            result = handle({**tool, "tool_name": name, "tool_input": {"token": self.w, "after_revision": 2}}, self.path)
            self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertEqual(handle({**tool, "session_id": "other"}, self.path), {})
        self.b.send(self.o, "close", "Accepted", 2, "close", close_confirmed=True)
        self.assertEqual(handle(tool, self.path), {})
        self.assertEqual(handle({"session_id": "w", "hook_event_name": "Stop"}, self.path), {})

    def test_session_can_join_new_channel_after_close(self):
        self.b.send(self.o, "close", "No task needed", 0, "close", close_confirmed=True)
        self.b.register("next", "worker", "w")
        self.assertEqual(self.b.status(self.o)["messages"][0]["text"], "No task needed")

    def test_wait_wakes_on_peer_message(self):
        async def scenario():
            waiting = asyncio.create_task(wait_for_message(self.w, 0, 3, self.path))
            await asyncio.sleep(0.05)
            self.assertFalse(waiting.done())
            self.task()
            result = await waiting
            self.assertEqual(result["messages"][0]["text"], "Do task one")
        asyncio.run(scenario())

    def test_orchestrator_wakes_when_worker_joins(self):
        o = self.b.register("later", "orchestrator", "later-o")["token"]
        async def scenario():
            waiting = asyncio.create_task(wait_for_message(o, 0, 3, self.path))
            await asyncio.sleep(0.05)
            self.assertFalse(waiting.done())
            self.b.register("later", "worker", "later-w")
            self.assertEqual((await waiting)["state"], "ready")
        asyncio.run(scenario())

    def test_hook_rejects_wrong_session_and_token(self):
        base = {"session_id": "w", "hook_event_name": "PreToolUse"}
        result = handle({**base, "tool_name": "mcp__agent_talks__register", "tool_input": {"session_id": "o"}}, self.path)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        result = handle({**base, "tool_name": "mcp__agent_talks__send", "tool_input": {"token": self.o}}, self.path)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_console_mirror_only_on_successful_send(self):
        message = self.task()
        data = {"session_id": "o", "hook_event_name": "PostToolUse", "tool_name": "mcp__agent_talks__send",
                "tool_response": {"isError": False, "structuredContent": message}}
        result = handle(data, self.path)
        self.assertEqual(result["systemMessage"], "Agent Talks [123] orchestrator → worker:\nDo task one")
        self.assertEqual(handle({**data, "tool_response": {"isError": True}}, self.path), {})

    def test_cancel_wait_keeps_message(self):
        async def scenario():
            waiting = asyncio.create_task(wait_for_message(self.w, 0, 3, self.path))
            await asyncio.sleep(0.05)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.task()
            result = await wait_for_message(self.w, 0, 1, self.path)
            self.assertEqual(result["revision"], 1)
        asyncio.run(scenario())

    def test_timeout_never_grants_work(self):
        result = asyncio.run(wait_for_message(self.w, 0, 1, self.path))
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["state"], "ready")
        self.assertIsNotNone(self.b.guard("w", "Bash"))

    def test_single_turn_owner_and_orchestrator_guard(self):
        self.assertEqual(self.b.status(self.o)["turn"], "orchestrator")
        self.task()
        for token in (self.o, self.w):
            self.assertEqual(self.b.status(token)["turn"], "worker")
        self.assertFalse(self.b.status(self.o)["your_turn"])
        self.assertTrue(self.b.status(self.w)["your_turn"])
        self.assertIsNotNone(self.b.guard("o", "Bash"))
        self.assertIsNone(self.b.guard("w", "Bash"))
        self.b.send(self.w, "report", "Done", 1, "report")
        self.assertIsNone(self.b.guard("o", "Bash"))
        self.assertIsNotNone(self.b.guard("w", "Bash"))

    def test_review_does_not_force_orchestrator_to_wait_or_close(self):
        self.task()
        self.b.send(self.w, "report", "Done", 1, "report")
        self.assertEqual(handle({"session_id": "o", "hook_event_name": "Stop"}, self.path), {})

    def test_close_requires_human_confirmation_even_for_legacy_writer(self):
        with self.assertRaises(CloseConfirmationRequired):
            self.b.send(self.o, "close", "Finished", 0, "close")
        with self.assertRaises(sqlite3.IntegrityError):
            self.b.db.execute("UPDATE channels SET state='closed' WHERE name='123'")
        self.assertEqual(self.b.status(self.o)["state"], "ready")
        self.b.send(self.o, "close", "Finished", 0, "close", close_confirmed=True)
        self.assertEqual(self.b.status(self.o)["state"], "closed")
        self.assertEqual(self.b.db.execute("SELECT COUNT(*) FROM close_approvals").fetchone()[0], 0)

    def test_reopen_preserves_members_and_history_and_expires_old_close(self):
        self.b.send(self.o, "close", "Finished", 0, "close", close_confirmed=True)
        before = self.b.status(self.o)["members"]
        result = self.b.reopen("123")
        self.assertEqual(result["turn"], "orchestrator")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(self.b.status(self.o)["members"], before)
        self.assertEqual(self.b.receive(self.o, 1)[0]["kind"], "reopen")
        self.assertIsNotNone(self.b.guard("w", "Bash"))
        self.assertFalse(self.b.reopen("123")["reopened"])
        with self.assertRaises(ValueError):
            self.b.send(self.o, "close", "Finished", 0, "close")
        self.b.send(self.o, "task", "Continue the work", 2, "next")

    def test_human_close_checks_revision_and_reopen_rejects_changed_members(self):
        self.task()
        with self.assertRaises(ValueError):
            self.b.human_close("123", 0)
        self.b.human_close("123", 1)
        self.b.register("different", "worker", "w")
        with self.assertRaises(ValueError):
            self.b.reopen("123")

    def test_close_confirmation_cannot_apply_after_channel_changes(self):
        class Context:
            async def elicit(context, question, schema):
                self.task()
                return SimpleNamespace(action="accept", data=SimpleNamespace(confirm=True))
        with patch("server.Broker", lambda: Broker(self.path)):
            with self.assertRaisesRegex(ToolError, "Stale revision"):
                asyncio.run(send_tool(self.o, "close", "Finished", 0, "close", Context()))
        self.assertEqual(self.b.status(self.o)["state"], "working")
        self.assertEqual(self.b.db.execute("SELECT COUNT(*) FROM close_approvals").fetchone()[0], 0)

    def test_missing_confirmation_ui_leaves_channel_open(self):
        class Context:
            async def elicit(context, question, schema):
                raise RuntimeError("No confirmation UI")
        with patch("server.Broker", lambda: Broker(self.path)):
            result = asyncio.run(send_tool(self.o, "close", "Finished", 0, "close", Context()))
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(self.b.status(self.o)["state"], "ready")

    def test_orchestrator_cannot_sleep_past_consumed_report(self):
        self.task()
        self.b.send(self.w, "report", "Done", 1, "report")
        result = asyncio.run(wait_for_message(self.o, 2, 1, self.path))
        self.assertTrue(result["your_turn"])
        self.assertEqual(result["messages"][0]["text"], "Done")
        self.assertEqual(self.b.status(self.o)["revision"], 2)

    def test_both_waiting_returns_control_to_orchestrator(self):
        self.task()
        async def scenario():
            orchestrator = asyncio.create_task(wait_for_message(self.o, 1, 3, self.path))
            await asyncio.sleep(0.05)
            self.assertFalse(orchestrator.done())
            worker = asyncio.create_task(wait_for_message(self.w, 1, 3, self.path))
            result = await asyncio.wait_for(orchestrator, 1)
            self.assertEqual(result["turn"], "orchestrator")
            self.assertEqual(result["messages"][0]["kind"], "recovery")
            self.assertEqual(result["messages"][0]["sender"], "system")
            self.assertFalse(worker.done())
            self.assertIsNotNone(self.b.guard("w", "Bash"))
            self.b.send(self.o, "task", "Please send the missing result", 2, "follow-up")
            result = await worker
            self.assertTrue(result["your_turn"])
            self.assertEqual(result["messages"][0]["text"], "Please send the missing result")
        asyncio.run(scenario())

    def test_unread_task_is_delivered_without_recovery(self):
        self.task()
        result = asyncio.run(wait_for_message(self.w, 0, 1, self.path))
        self.assertTrue(result["your_turn"])
        self.assertEqual(result["messages"][0]["kind"], "task")
        self.assertEqual(self.b.status(self.w)["revision"], 1)

    def test_hook_repairs_old_server_wait_arguments(self):
        self.task()
        self.b.send(self.w, "report", "Done", 1, "report")
        result = handle({"session_id": "o", "hook_event_name": "PreToolUse",
                         "tool_name": "mcp__agent_talks__wait",
                         "tool_input": {"token": self.o, "after_revision": 2, "timeout_seconds": 3600}}, self.path)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(output["updatedInput"]["after_revision"], 1)
        self.assertEqual(output["updatedInput"]["timeout_seconds"], 3600)

    def test_hook_yields_worker_for_old_server(self):
        self.task()
        handle({"session_id": "w", "hook_event_name": "PreToolUse",
                "tool_name": "mcp__agent_talks__wait",
                "tool_input": {"token": self.w, "after_revision": 1}}, self.path)
        self.assertEqual(self.b.status(self.o)["turn"], "orchestrator")
        self.b.prepare_wait(self.w, 1)
        self.assertEqual(self.b.status(self.o)["revision"], 2)

    def test_recovery_stales_old_work_and_preserves_retries(self):
        first = self.task()
        self.b.prepare_wait(self.w, 1)
        self.assertEqual(self.task(), first)
        with self.assertRaises(ValueError):
            self.b.send(self.w, "report", "Late report", 1, "late")
        self.assertEqual(self.b.status(self.o)["revision"], 2)

    def test_concurrent_recovery_happens_once(self):
        self.task()
        def recover(_):
            b = Broker(self.path)
            try:
                return b.prepare_wait(self.w, 1)
            finally:
                b.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(recover, [1, 2]))
        self.assertEqual(self.b.status(self.o)["revision"], 2)
        self.assertEqual(len(self.b.receive(self.o, 1)), 1)


if __name__ == "__main__":
    unittest.main()
