"""Контракт печатного режима Claude Code, включая учёт и продолжение конкретного диалога."""
from pathlib import Path
import subprocess
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit.runtimes import claude_chief


class ClaudeChiefTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.session = str(uuid4())

    def test_probe_checks_installed_flags_without_starting_a_session(self):
        help_text = '-p, --print\n--input-format <format>\n--output-format text|json|stream-json\n--verbose\n--resume\n--session-id'
        with patch.object(claude_chief, '_executable', return_value='tool'), patch.object(subprocess, 'run') as run:
            run.side_effect = [subprocess.CompletedProcess(['tool'], 0, stdout=s) for s in ('2.1 (Claude Code)', help_text)]
            info = claude_chief.probe()
            self.assertTrue(info['available'])
            self.assertEqual(info['integration_scope'], 'chief_turn')
            self.assertEqual(info['worker_guarantees'], dict(
                assigned_file_only=False, family_stop_confirmed=False, family_queue_control=False,
                family_export=False, family_crash_reconciliation=False))
            self.assertTrue(all(info['capabilities'].values()))
            self.assertEqual([[*call.args[0]] for call in run.call_args_list], [['tool','--version'],['tool','--help']])
            self.assertTrue(all(call.kwargs['stdin'] == subprocess.DEVNULL for call in run.call_args_list))

    def test_missing_tool_incomplete_help_and_timeout_are_distinct(self):
        with patch.object(claude_chief, '_executable', return_value=None):
            self.assertIn('не найдена', claude_chief.probe()['error'])
            with self.assertRaises(FileNotFoundError):
                claude_chief.command(self.root, session=None, resume=False)
        with patch.object(claude_chief, '_executable', return_value='tool'):
            with patch.object(claude_chief, '_inspect', side_effect=['2.1', '--print --output-format stream-json']):
                info = claude_chief.probe()
                self.assertFalse(info['available'])
                self.assertFalse(info['capabilities']['resume_by_id'])
            with patch.object(claude_chief, '_inspect', side_effect=subprocess.TimeoutExpired('tool',5)):
                self.assertIn('5 секунд', claude_chief.probe()['error'])

    def test_explicit_tool_is_project_independent_and_does_not_fall_back(self):
        executable = self.root / 'claude.exe'
        executable.touch()
        executable.chmod(0o700)
        with patch.dict(os.environ, CHIEF_CLAUDE_EXECUTABLE=str(executable)):
            self.assertEqual(claude_chief._executable(), str(executable))
        with patch.dict(os.environ, CHIEF_CLAUDE_EXECUTABLE='relative/claude.exe'):
            self.assertIsNone(claude_chief._executable())

    def test_new_and_resumed_commands_preserve_default_permissions(self):
        with patch.object(claude_chief, '_executable', return_value='path/Claude Code'):
            flags = ['path/Claude Code','-p','--input-format','text','--output-format','stream-json','--verbose']
            self.assertEqual(claude_chief.command(self.root, session=None, resume=False), flags)
            for resume, flag in [(False, '--session-id'),(True, '--resume')]:
                self.assertEqual(claude_chief.command(self.root, session=self.session, resume=resume),
                                 flags + [flag, self.session])
            for value in (None, '', '--continue', 'текст вместо UUID', 0):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    claude_chief.command(self.root, session=value, resume=True)
            with self.assertRaises(ValueError):
                claude_chief.command(self.root / 'ещё нет', session=None, resume=False)

    def test_final_usage_and_text_belong_to_the_root_result(self):
        usage = dict(input_tokens=11,output_tokens=0,cache_read_input_tokens=5,cache_creation_input_tokens=3)
        event = dict(type='result', subtype='success', is_error=False, session_id=self.session,
                     result='Готово',usage=usage)
        self.assertEqual(claude_chief.parse_event(event),
                         dict(completed=True,session=self.session,text='Готово',
                              usage=dict(input_tokens=11,output_tokens=0,cached_input_tokens=5,cache_creation_input_tokens=3)))
        self.assertEqual(claude_chief.parse_event(dict(type='system',subtype='init',session_id=self.session)),
                         {'session':self.session})
        for change in ({'type':'assistant'}, {'parent_tool_use_id':'tool123'}, {'agent_id':'subagent'}):
            self.assertEqual(claude_chief.parse_event(event | change), {})

    def test_failures_and_unknown_terminal_results_do_not_complete_the_task(self):
        base = dict(type='result',subtype='error_max_turns',session_id=self.session,errors=['Достигнут лимит'],is_error=True)
        self.assertEqual(claude_chief.parse_event(base),
                         dict(session=self.session,failed=True,error='Достигнут лимит'))
        invalid_usage = {'input_tokens': True, 'output_tokens': -1, 'cache_read_input_tokens': None}
        for change in ({'subtype':'success','is_error':True},{'subtype':'новый_статус'},{'session_id':'не ID'}):
            parsed = claude_chief.parse_event(base | change | {'usage':invalid_usage})
            self.assertTrue(parsed.get('failed'))
            self.assertNotIn('completed',parsed)
            self.assertNotIn('usage',parsed)
