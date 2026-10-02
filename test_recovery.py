import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from broker import Broker
from hook import handle
from server import register, wait_for_message


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite'
        self.b = Broker(self.path)
        self.o = self.b.register('pair', 'orchestrator', 'old-o')['token']
        self.w = self.b.register('pair', 'worker', 'old-w')['token']
        self.b.send(self.o, 'task', 'An unfinished task', 0, 'task')

    def tearDown(self):
        self.b.db.close()
        self.temp.cleanup()

    def recover(self, **roles):
        return self.b.recover(self.b.recovery_plan('pair', roles), confirmed=True)

    def test_same_session_recovers_token_without_mutating_turn(self):
        self.assertEqual(self.b.register('pair', 'worker', 'old-w')['token'], self.w)
        self.assertEqual(self.b.status(self.w)['revision'], 1)
        self.assertEqual(self.b.status(self.w)['turn'], 'worker')
        result = handle({'hook_event_name': 'PreToolUse', 'session_id': 'old-w',
                         'tool_name': 'mcp__agent_talks__status', 'tool_input': {'token': 'lost'}}, self.path)
        self.assertIn('channel=pair, role=worker, session_id=old-w',
                      result['hookSpecificOutput']['permissionDecisionReason'])

    def test_either_or_both_can_be_replaced_with_history_intact(self):
        original = dict(self.b.db.execute('SELECT * FROM messages').fetchone())
        self.recover(worker='new-w')
        with self.assertRaises(ValueError):
            self.b.send(self.w, 'report', 'Late old result', 1, 'late')
        new_w = self.b.register('pair', 'worker', 'new-w')['token']
        self.assertNotEqual(new_w, self.w)
        self.assertEqual(self.b.status(new_w)['turn'], 'orchestrator')
        self.assertEqual(dict(self.b.db.execute('SELECT * FROM messages WHERE id=?', (original['id'],)).fetchone()), original)
        self.recover(orchestrator='new-o')
        with self.assertRaises(ValueError):
            self.b.status(self.o)
        self.recover(orchestrator='both-o', worker='both-w')
        o = self.b.register('pair', 'orchestrator', 'both-o')['token']
        w = self.b.register('pair', 'worker', 'both-w')['token']
        self.assertEqual(self.b.status(o)['revision'], 4)
        self.b.send(o, 'task', 'Review partial work first', 4, 'resume')
        self.assertEqual(self.b.receive(w, 4)[0]['text'], 'Review partial work first')
        self.b.send(w, 'report', 'Reviewed', 5, 'result')
        self.assertEqual(self.b.status(o)['turn'], 'orchestrator')

    def test_old_session_guard_stays_blocked_and_can_stop(self):
        self.recover(worker='new-w')
        for name in ['Bash', 'mcp__agent_talks__register', 'mcp__agent_talks__send']:
            result = handle({'hook_event_name': 'PreToolUse', 'session_id': 'old-w',
                             'tool_name': name, 'tool_input': {}}, self.path)
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(handle({'hook_event_name': 'Stop', 'session_id': 'old-w'}, self.path), {})

    def test_confirmation_and_snapshot_required(self):
        plan = self.b.recovery_plan('pair', {'worker': 'new-w'})
        with self.assertRaises(ValueError):
            self.b.recover(plan)
        self.b.send(self.w, 'report', 'Finished while confirmation was pending', 1, 'done')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.b.recover(plan, confirmed=True)
        self.assertEqual(self.b.register('pair', 'worker', 'old-w')['token'], self.w)

    def test_concurrent_replacements_only_one_wins(self):
        plans = [self.b.recovery_plan('pair', {'worker': f'new-{n}'}) for n in range(2)]
        def run(plan):
            b = Broker(self.path)
            try:
                b.recover(plan, confirmed=True)
                return True
            except ValueError:
                return False
            finally:
                b.db.close()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(run, plans)), 1)

    def test_no_role_swaps_or_other_channel_takeover(self):
        self.b.register('other', 'worker', 'elsewhere')
        for roles in ({'worker': 'old-o'}, {'worker': 'elsewhere'},
                      {'worker': 'same', 'orchestrator': 'same'}, {}):
            with self.assertRaises(ValueError):
                self.b.recovery_plan('pair', roles)
        self.assertEqual(self.b.status(self.w)['state'], 'working')

    def test_recovery_keeps_closed_and_does_not_rotate_same_session(self):
        self.b.human_close('pair', 1)
        self.recover(worker='new-w')
        w = self.b.register('pair', 'worker', 'new-w')['token']
        self.assertEqual(self.b.status(w)['state'], 'closed')
        revision = self.b.status(w)['revision']
        self.recover(worker='new-w')
        self.assertEqual(self.b.register('pair', 'worker', 'new-w')['token'], w)
        self.assertEqual(self.b.status(w)['revision'], revision)
        self.b.reopen('pair')
        self.assertEqual(self.b.status(w)['turn'], 'orchestrator')

    def test_waiting_peer_wakes_and_replaced_wait_fails(self):
        async def run():
            waiting = asyncio.create_task(wait_for_message(self.o, 1, 5, self.path))
            await asyncio.sleep(0.05)
            self.recover(worker='new-w')
            result = await asyncio.wait_for(waiting, 2)
            self.assertTrue(result['your_turn'])
            self.assertEqual(result['messages'][0]['kind'], 'recovery')
            with self.assertRaises(ValueError):
                await wait_for_message(self.w, 1, 1, self.path)
        asyncio.run(run())

    def test_register_tool_handles_confirmation_outcomes(self):
        async def run():
            for action, confirm in [('decline', None), ('cancel', None), ('accept', False), ('error', None)]:
                async def elicit(*args):
                    if action == 'error':
                        raise RuntimeError('No UI')
                    return SimpleNamespace(action=action, data=SimpleNamespace(confirm=confirm))
                with patch('server.Broker', side_effect=lambda: Broker(self.path)):
                    result = await register('pair', 'worker', 'new-w', SimpleNamespace(elicit=elicit))
                self.assertIn(result['status'], ['recovery_cancelled', 'recovery_confirmation_required'])
                self.assertEqual(self.b.register('pair', 'worker', 'old-w')['token'], self.w)
            async def accept(*args):
                return SimpleNamespace(action='accept', data=SimpleNamespace(confirm=True))
            with patch('server.Broker', side_effect=lambda: Broker(self.path)):
                result = await register('pair', 'worker', 'new-w', SimpleNamespace(elicit=accept))
            self.assertEqual(result['session'], 'new-w')
            self.assertEqual(result['status']['turn'], 'orchestrator')
        asyncio.run(run())
