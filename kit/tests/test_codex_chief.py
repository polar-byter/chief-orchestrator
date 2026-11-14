"""Контракт запуска и наблюдаемых результатов нативного Codex CLI."""
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit.runtimes import codex_chief as chief


class CodexChiefTests(unittest.TestCase):
    def test_probe_reads_installed_command_help_without_model_or_project(self):
        outputs = iter(['codex-cli 0.X', 'exec --cd --json stdin', 'resume SESSION_ID --json stdin'])
        with patch.object(chief, '_executable', return_value='/bin/codex'), patch.object(
                chief, '_help', side_effect=lambda *args: next(outputs)) as calls:
            result = chief.probe()
        self.assertTrue(result['available'])
        self.assertEqual(result['integration_scope'], 'chief_turn')
        self.assertEqual(result['worker_guarantees'], dict(
            assigned_file_only=False, family_stop_confirmed=False, family_queue_control=False,
            family_export=False, family_crash_reconciliation=False))
        self.assertTrue(all(result['capabilities'].values()))
        self.assertEqual(result['version'], 'codex-cli 0.X')
        self.assertEqual([c.args[1:] for c in calls.call_args_list],
                         [('--version',), ('exec', '--help'), ('exec', 'resume', '--help')])
        with patch.object(chief, '_executable', return_value=None):
            self.assertFalse(chief.probe()['available'])
            with self.assertRaises(FileNotFoundError):
                chief.command(Path.cwd(), session=None, resume=False)

    def test_new_thread_and_exact_resume_keep_configured_model_and_permissions(self):
        root = Path('Проект + тест').resolve()
        identifier = str(uuid4())
        with patch.object(chief, '_executable', return_value='codex'):
            self.assertEqual(chief.command(root, session=None, resume=False),
                             ['codex', 'exec', '--cd', str(root), '--json', '-'])
            self.assertEqual(chief.command(root, session=identifier, resume=True),
                             ['codex', 'exec', '--cd', str(root), 'resume','--json', identifier, '-'])
            for sid, resume in [(None, True), ('--last', True), (identifier, False)]:
                with self.subTest(sid=sid, resume=resume), self.assertRaises(ValueError):
                    chief.command(root, session=sid, resume=resume)

    def test_outcome_comes_from_turn_and_counts_only_observed_usage(self):
        sid = str(uuid4())
        self.assertEqual(chief.parse_event(dict(type='thread.started', thread_id=sid)), {'session':sid})
        self.assertEqual(chief.parse_event(dict(type='turn.started')), {})
        self.assertEqual(chief.parse_event(dict(type='item.completed', item=dict(type='command_execution', text='cmd'))), {})
        self.assertEqual(chief.parse_event(dict(type='item.completed', item=dict(type='agent_message', text='Принял'))),
                         {'text':'Принял'})
        self.assertEqual(chief.parse_event(dict(type='error', message='Переподключение')), {'error':'Переподключение'})
        self.assertEqual(chief.parse_event(dict(type='turn.failed', error=dict(message='Отказ'))),
                         {'failed':True, 'error':'Отказ'})
        self.assertEqual(chief.parse_event(dict(type='turn.completed')), {'completed':True})
        usage = dict(input_tokens=0, cached_input_tokens=False, output_tokens=4, reasoning_output_tokens=None)
        self.assertEqual(chief.parse_event(dict(type='turn.completed', usage=usage)),
                         dict(completed=True, usage={'input_tokens':0,'output_tokens':4}))
