"""Доставка из очереди: реальные файлы, процессы и общий путь из Obsidian и CLI."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit import chief_mode, native_chief, project_docs
from kit.runtimes import native_process
from kit.project_dispatch import dispatch_request
from kit.project_inbox import process_inbox
from kit.project_revisions import pending_requests, request_revision
from kit.project_vault import ProjectVault
from kit.task_metrics import TaskMetrics


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'Проект'
        self.root.mkdir()
        self.vault = ProjectVault.connect(self.root)
        self.vault.add('stage', 'stage', 'Этап')
        self.note = self.vault.workspace('stage')
        self.template = self.note.read_text(encoding='utf-8')
        self.commands = []
        self.mode = 'completed'
        for agent, transport in native_chief.TRANSPORTS.items():
            command = patch.object(transport, 'command', self.fake_command(agent))
            command.start()
            self.addCleanup(command.stop)

    def fake_command(self, agent):
        def command(root, *, session, resume):
            self.commands.append((agent, root, session, resume))
            sid = session or str(uuid4())
            if self.mode == 'wrong_session':
                sid = str(uuid4())
            if agent == 'codex':
                rows = [dict(type='thread.started', thread_id=sid),
                        dict(type='item.completed', item=dict(type='agent_message', text='Ответ по заявке')),
                        dict(type='turn.completed', usage=dict(input_tokens=15, output_tokens=3))]
                if self.mode == 'failed':
                    rows[-1] = dict(type='turn.failed', error=dict(message='ошибка модели'))
            else:
                rows = [dict(type='system', subtype='init', session_id=sid),
                        dict(type='result', subtype='success', session_id=sid, result='Ответ по заявке',
                             is_error=False, usage=dict(input_tokens=15, output_tokens=3))]
            stream = '\n'.join(json.dumps(row) for row in rows)
            source = ('import sys; p=sys.stdin.buffer.read().decode(); '
                      'assert "Изменение, переданное пользователем" in p; '
                      f'print({stream!r}); sys.exit({int(self.mode == "failed")})')
            return [sys.executable, '-c', source]
        return command

    def cli(self, *args):
        return project_docs.execute(project_docs._parser().parse_args(['--root', str(self.root), *args]))

    def request(self, text='Подтвердите получение.'):
        self.note.write_text(self.template + text, encoding='utf-8')
        return request_revision(self.vault, 'stage')

    def test_default_and_explicit_choice_persist_per_project(self):
        self.assertEqual(self.cli('configure')['agent'], 'default')
        self.cli('configure', '--agent', 'claude')
        self.assertEqual(native_chief.agent_for(ProjectVault.load(self.root)), 'claude')
        other = self.root.parent / 'Другой проект'
        other.mkdir()
        self.assertEqual(native_chief.agent_for(ProjectVault.connect(other)), 'default')
        self.assertEqual(self.cli('configure', '--agent', 'default')['agent'], 'default')
        self.assertEqual(self.commands, [])

    def test_existing_gateway_journal_never_silently_changes_recipient(self):
        request = self.request()
        TaskMetrics(self.root).start(request['task_id'], session='ses_existing', title='Начато в шлюзе')
        self.cli('configure', '--agent', 'claude')
        with patch('kit.project_dispatch.create_session_gateway') as gateway:
            with self.assertRaisesRegex(ValueError, 'уже создан журнал шлюза'):
                dispatch_request(self.vault, request)
        gateway.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_watcher_delivers_once_cli_reads_same_receipt_after_agent_switch(self):
        self.cli('configure', '--agent', 'codex')
        self.note.write_text(self.template.replace('- [ ] Передать Чифу', '- [x] Передать Чифу') + 'Ответьте.', encoding='utf-8')
        note, = process_inbox(self.vault)
        request, = pending_requests(self.vault)
        self.cli('configure', '--agent', 'claude')
        receipt = self.cli('send', '--request', request['id'])
        self.assertIn(receipt['session'], note.read_text(encoding='utf-8'))
        folder = self.root / '.metrics' / request['task_id']
        self.assertEqual(receipt, json.loads((folder / 'documentation-delivery.json').read_text(encoding='utf-8')))
        self.assertEqual(receipt, json.loads((folder / 'native-turn.json').read_text(encoding='utf-8')))
        self.assertEqual(receipt['project'], self.vault.project_id)
        self.assertEqual((receipt['exit_code'], receipt['usage_scope']), (0, 'session_reported_total'))
        self.assertEqual(receipt['text'], 'Ответ по заявке')
        self.assertFalse((folder / 'events.jsonl').exists())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(dispatch_request(self.vault, request), receipt)
        self.assertEqual(len(self.commands), 1)

    def test_continuation_requires_same_agent_project_record_and_new_pending_request(self):
        self.cli('configure', '--agent', 'claude')
        first = self.request()
        initial = self.cli('send', '--request', first['id'])
        next_request = self.request('Уточните ответ.')
        with self.assertRaisesRegex(ValueError, 'сохранённую открытую'):
            dispatch_request(self.vault, first)
        self.cli('configure', '--agent', 'codex')
        with self.assertRaisesRegex(ValueError, 'подтверждённая'):
            self.cli('send', '--request', next_request['id'], '--resume', initial['session'])
        self.cli('configure', '--agent', 'claude')
        actual = self.cli('send', '--request', next_request['id'], '--resume', initial['session'])
        self.assertEqual(actual['session'], initial['session'])
        self.assertEqual(actual['usage_scope'], 'cli_result')
        self.assertEqual(self.cli('send', '--request', next_request['id']), actual)
        self.assertEqual(self.commands[-1], ('claude', self.root, initial['session'], True))
        with self.assertRaisesRegex(ValueError, 'другому ходу'):
            self.cli('send', '--request', next_request['id'], '--resume', str(uuid4()))

    def test_refuses_wrong_session_and_keeps_immutable_failed_turn(self):
        self.cli('configure', '--agent', 'claude')
        self.mode = 'wrong_session'
        request = self.request()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'UUID другой'):
                dispatch_request(self.vault, request)
        folder = self.root / '.metrics' / request['task_id']
        self.assertEqual(json.loads((folder / 'native-turn.json').read_text(encoding='utf-8'))['status'], 'uncertain')
        self.assertFalse((folder / 'documentation-delivery.json').exists())
        self.assertEqual(len(self.commands), 1)

    def test_failure_and_crash_before_final_receipt_never_restart_cli(self):
        self.cli('configure', '--agent', 'codex')
        self.mode = 'failed'
        request = self.request()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'ошибка модели'):
                dispatch_request(self.vault, request)
        self.assertEqual(len(self.commands), 1)
        request = self.request('Другой ход.')
        self.mode = 'completed'
        publish = native_chief._publish
        def crash(folder, name, data):
            if name == 'documentation-delivery.json':
                raise OSError('питание отключено')
            return publish(folder, name, data)
        with patch.object(native_chief, '_publish', side_effect=crash), self.assertRaisesRegex(OSError, 'питание'):
            dispatch_request(self.vault, request)
        self.assertEqual(dispatch_request(self.vault, request)['status'], 'completed')
        self.assertEqual(len(self.commands), 2)

    def test_changed_payload_and_ambiguous_admission_do_not_send(self):
        request = self.request()
        self.cli('configure', '--agent', 'codex')
        with self.assertRaisesRegex(ValueError, 'сохранённую открытую'):
            dispatch_request(self.vault, dict(request, text='Подменили сохранённый текст.'))
        with patch.object(native_process, 'supported', return_value=False), patch(
                'kit.native_chief_process.subprocess.Popen', side_effect=SystemExit), self.assertRaises(SystemExit):
            dispatch_request(self.vault, request)
        self.cli('configure', '--agent', 'default')
        with self.assertRaisesRegex(ValueError, 'Результат передачи неизвестен'):
            dispatch_request(self.vault, request)
        self.assertEqual(len(self.commands), 1)

    def test_cli_could_not_start_then_safe_retry_uses_same_request(self):
        request = self.request()
        self.cli('configure', '--agent', 'claude')
        folder = self.root / '.metrics' / request['id']
        with patch.object(native_process, 'supported', return_value=False), patch(
                'kit.native_chief_process.subprocess.Popen', side_effect=FileNotFoundError), self.assertRaises(FileNotFoundError):
            dispatch_request(self.vault, request)
        self.assertFalse(folder.exists())
        self.assertEqual(dispatch_request(self.vault, request)['status'], 'completed')

    def test_timeout_blocks_redelivery_and_acceptance(self):
        request = self.request()
        self.cli('configure', '--agent', 'codex')
        source = "import time; time.sleep(5)"
        with patch.object(native_chief, 'TURN_TIMEOUT', 0.2), patch.object(native_chief.TRANSPORTS['codex'], 'command',
                return_value=[sys.executable, '-c', source]) as command:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'не завершил ход'):
                    dispatch_request(self.vault, request)
        command.assert_called_once()
        folder = self.root / '.metrics' / request['id']
        outcome = json.loads((folder / 'native-turn.json').read_text(encoding='utf-8'))
        self.assertEqual(outcome['status'], 'uncertain')
        self.assertIsNotNone(outcome['exit_code'])
        self.assertFalse((folder / 'documentation-delivery.json').exists())

    def test_switch_off_confirms_active_direct_turn_and_prevents_redelivery(self):
        if not native_process.supported():
            self.skipTest('Требуется доступная группа процессов ОС.')
        self.cli('configure', '--agent', 'claude')
        request = self.request()
        folder = self.root / '.metrics' / request['task_id']
        heartbeat = self.root / 'started'
        child = ('from pathlib import Path; import sys,time; '
                 'Path(sys.argv[1]).touch(); time.sleep(30)')
        with patch.object(native_chief.TRANSPORTS['claude'], 'command',
                          return_value=[sys.executable, '-c', child, str(heartbeat)]) as command:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(dispatch_request, self.vault, request)
                deadline = time.monotonic() + 10
                while not heartbeat.is_file() and not future.done() and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertTrue(heartbeat.is_file())
                self.assertFalse(chief_mode.set_mode(self.root, False)['enabled'])
                with self.assertRaisesRegex(ValueError, 'остановлен при выключении'):
                    future.result(timeout=5)
            attempt = json.loads((folder / 'documentation-sending.json').read_bytes())
            self.assertTrue(native_process.stop_and_confirm(attempt['process_boundary']))
            chief_mode.set_mode(self.root, True)
            with self.assertRaisesRegex(ValueError, 'остановлен при выключении'):
                dispatch_request(self.vault, request)
            command.assert_called_once()

    def test_cannot_continue_session_for_another_feature_or_project(self):
        first = self.request()
        self.cli('configure', '--agent', 'codex')
        session = dispatch_request(self.vault, first)['session']
        self.vault.add('feature', 'feature', 'Фича', 'stage')
        other = self.root.parent / 'Отдельный проект'
        other.mkdir()
        foreign = ProjectVault.connect(other)
        foreign.add('stage', 'stage', 'Этап')
        for vault, key in ((self.vault, 'feature'), (foreign, 'stage')):
            native_chief.configure(vault, 'codex')
            with vault.workspace(key).open('a', encoding='utf-8') as stream:
                stream.write('Продолжить разговор.')
            with self.assertRaisesRegex(ValueError, 'подтверждённая'):
                dispatch_request(vault, request_revision(vault, key), resume=session)
