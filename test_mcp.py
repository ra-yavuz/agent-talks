"""Real two-process MCP round trip. Uses no models or live agent sessions."""
import asyncio
from contextlib import AsyncExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.types import ElicitResult
from mcp.client.stdio import stdio_client

from broker import CLOSE_QUESTION


class MCPRoundTrip(unittest.TestCase):
    def test_two_processes(self):
        asyncio.run(self.run_pair())

    async def run_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            async with AsyncExitStack() as stack:
                answers = [("decline", None), ("cancel", None), ("accept", {"confirm": False}), ("accept", {"confirm": True})]
                questions = []
                async def confirm_close(context, params):
                    questions.append(params.message)
                    action, content = answers.pop(0)
                    return ElicitResult(action=action, content=content)

                async def client():
                    params = StdioServerParameters(command=sys.executable,
                        args=[str(Path(__file__).with_name("server.py"))],
                        env={**os.environ, "AGENT_TALKS_DB": str(Path(directory) / "state.sqlite")})
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=10,
                                                                          elicitation_callback=confirm_close))
                    await session.initialize()
                    return session

                orchestrator, worker = await client(), await client()

                async def call(client, name, **args):
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.is_error, str(result))
                    return result.structured_content

                o = await call(orchestrator, "register", channel="123", role="orchestrator", session_id="test-o")
                w = await call(worker, "register", channel="123", role="worker", session_id="test-w")
                invalid = await worker.call_tool("wait", {"token": w["token"], "after_revision": 99, "timeout_seconds": 1})
                self.assertTrue(invalid.is_error)
                self.assertIn("Read status", invalid.content[0].text)
                conflict = await worker.call_tool("register", {"channel": "another", "role": "worker", "session_id": "test-w"})
                self.assertTrue(conflict.is_error)
                self.assertIn("one active channel", conflict.content[0].text)
                waiting = asyncio.create_task(call(worker, "wait", token=w["token"], after_revision=0, timeout_seconds=5))
                await asyncio.sleep(0.1)
                self.assertFalse(waiting.done())
                await call(orchestrator, "send", token=o["token"], kind="task", text="Check the result", revision=0, request_id="task")
                received = await waiting
                self.assertEqual(received["messages"][0]["text"], "Check the result")
                waiting = asyncio.create_task(call(orchestrator, "wait", token=o["token"], after_revision=1, timeout_seconds=5))
                await call(worker, "send", token=w["token"], kind="report", text="Checked", revision=1, request_id="report")
                self.assertEqual((await waiting)["messages"][0]["text"], "Checked")
                for _ in range(3):
                    result = await call(orchestrator, "send", token=o["token"], kind="close", text="Accepted", revision=2, request_id="close")
                    self.assertEqual(result["status"], "close_cancelled")
                    self.assertEqual((await call(orchestrator, "status", token=o["token"]))["state"], "review")
                await call(orchestrator, "send", token=o["token"], kind="close", text="Accepted", revision=2, request_id="close")
                self.assertEqual(len(questions), 4)
                self.assertTrue(all(CLOSE_QUESTION in question for question in questions))
                self.assertEqual((await call(worker, "wait", token=w["token"], after_revision=2, timeout_seconds=1))["state"], "closed")
                reopened = await call(orchestrator, "reopen", token=o["token"])
                self.assertTrue(reopened["reopened"])
                self.assertEqual(reopened["turn"], "orchestrator")
                ready = await call(orchestrator, "wait", token=o["token"], after_revision=4, timeout_seconds=1)
                self.assertTrue(ready["your_turn"])
                await call(orchestrator, "send", token=o["token"], kind="task", text="Check again", revision=4, request_id="task-2")
                o_wait = asyncio.create_task(call(orchestrator, "wait", token=o["token"], after_revision=5, timeout_seconds=3))
                w_wait = asyncio.create_task(call(worker, "wait", token=w["token"], after_revision=5, timeout_seconds=3))
                recovered = await asyncio.wait_for(o_wait, 2)
                self.assertTrue(recovered["your_turn"])
                self.assertEqual(recovered["messages"][0]["kind"], "recovery")
                await call(orchestrator, "send", token=o["token"], kind="task", text="Send the missing report", revision=6, request_id="task-3")
                self.assertTrue((await w_wait)["your_turn"])
                # A fresh MCP connection can replace a lost session through the
                # same register tool, using native human confirmation.
                replacement = await client()
                answers.append(("accept", {"confirm": True}))
                recovered = await call(replacement, "register", channel="123", role="worker", session_id="replacement-w")
                self.assertNotEqual(recovered["token"], w["token"])
                self.assertEqual(recovered["status"]["turn"], "orchestrator")
                revoked = await worker.call_tool("status", {"token": w["token"]})
                self.assertTrue(revoked.is_error)
                self.assertIn("Register again", revoked.content[0].text)
                self.assertIn("Replace the previous session", questions[-1])
                state = await call(orchestrator, "status", token=o["token"])
                task = await call(orchestrator, "send", token=o["token"], kind="task", text="Review interrupted work", revision=state["revision"], request_id="replacement-task")
                await call(replacement, "send", token=recovered["token"], kind="report", text="Recovered", revision=task["revision"], request_id="replacement-report")


if __name__ == "__main__":
    unittest.main()
