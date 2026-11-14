"""Связь обсуждения и прогона с одной записью проектной базы."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from kit.journal import TaskJournal
from kit.project_revisions import accept_revision, request_revision
from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths
from kit.stage_collaboration import apply_action, blocking_threads, read_collaboration


class StageCollaborationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        project = Path(temp.name) / 'project'
        project.mkdir()
        self.vault = ProjectVault.connect(project)
        self.vault.add('stage', 'plan', 'Подготовка')
        self.vault.add('feature', 'capacity', 'Ёмкость', 'plan')
        self.document = section_paths(self.vault, 'capacity')['requirements']
        self.journal = TaskJournal(self.vault.root)

    def command(self, action, **fields):
        return dict(project_id=self.vault.project_id, operation_id=str(uuid4()), role='analyst', action=action, **fields)

    def ask(self):
        return self.command('ask', feature='capacity', revision=1, subject='INV-2', assignee='manager',
                            blocking=True, author='Нина', text='Кто подтверждает часы?')

    def blocked(self):
        with self.vault.locked() as db:
            return [t['id'] for t in blocking_threads(db, 'plan')]

    def test_questions_reopen_and_concurrent_retries_keep_single_history(self):
        first = self.ask()
        with self.vault.reading() as db:
            self.assertEqual(blocking_threads(db, 'plan'), [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda _: apply_action(self.vault, 'plan', first), range(2)))
        self.assertEqual(a, b)
        self.assertEqual(self.blocked(), [a['id']])
        answer = self.command('reply', thread_id=a['id'], assignee='analyst', expected_version=1, text='РП.')
        answer['role'] = 'manager'
        apply_action(self.vault, 'plan', answer)
        self.assertEqual(self.blocked(), [])
        with self.assertRaisesRegex(ValueError, 'изменилось'):
            apply_action(self.vault, 'plan', dict(answer, operation_id=str(uuid4())))
        reopen = self.command('reopen', thread_id=a['id'], assignee='engineer', expected_version=2,
                              text='Изменилась доступность инженера.')
        apply_action(self.vault, 'plan', reopen)
        self.assertEqual(self.blocked(), [a['id']])
        self.assertEqual(apply_action(self.vault, 'plan', first), a)
        with self.assertRaisesRegex(ValueError, 'другая операция'):
            apply_action(self.vault, 'plan', dict(first, subject='Другой вопрос'))
        view = read_collaboration(ProjectVault.load(self.vault.root), 'plan')
        self.assertEqual(len(view['threads']), 1)
        thread = view['threads'][0]
        self.assertEqual([m['action'] for m in thread['messages']], ['ask', 'reply', 'reopen'])
        self.assertEqual((thread['feature'], thread['revision'], thread['assignee'], thread['version']),
                         ('capacity', 1, 'engineer', 3))
        self.assertEqual(thread['messages'][0]['author'], 'Нина')

    def test_stage_project_and_revision_are_checked_before_writing(self):
        self.vault.add('stage', 'other', 'Разработка')
        ask = self.ask()
        for key, command in [('other', ask), ('plan', dict(ask, project_id=str(uuid4()))),
                             ('plan', dict(ask, revision=100)), ('capacity', ask), ('plan', dict(ask, role='admin'))]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_action(self.vault, key, command)
        self.assertEqual(read_collaboration(self.vault, 'plan')['threads'], [])
        ask.update(feature='', blocking=False)
        first = apply_action(self.vault, 'plan', ask)
        with self.assertRaisesRegex(ValueError, 'обсуждения'):
            apply_action(self.vault, 'other', self.command('reply', thread_id=first['id'], expected_version=1,
                                                            assignee='manager', text='Чужая тема'))
        self.assertEqual(self.blocked(), [])

    def make_run(self, task):
        folder = self.journal.task_dir(task)
        folder.mkdir()
        log = 'test-' + uuid4().hex + '.log'
        (folder / log).write_text('test_busy (kit.calendar.CalendarTests.test_busy) ... skipped \'нет календаря\'\n',
                                  encoding='utf-8')
        with self.journal.lock(task):
            self.journal.append(task, 'start', title='Проверка календаря')
            return self.journal.append(task, 'test', scope='full', status='skipped', tests=2,
                                       failures=0, errors=0, skipped=1, log=log)

    def trace(self, task, rule, **fields):
        return self.command('trace', feature='capacity', revision=1, section='requirements',
                            source='working', invariant='INV-2', task_id=task, rule=rule, **fields)

    def test_rule_snapshots_keep_their_basis_and_evidence_after_new_revision(self):
        run = self.make_run('calendar')
        self.document.write_text('INV-2. Срок зависит от доступности.', encoding='utf-8')
        request_note = self.vault.workspace('capacity')
        with request_note.open('a', encoding='utf-8') as f:
            f.write('Уточнить доступность.')
        request = request_revision(self.vault, 'capacity')
        working = self.trace('calendar', 'Срок зависит от доступности.', test_event_id=run['event_id'])
        saved = apply_action(self.vault, 'plan', working)
        self.assertEqual(saved['test']['skipped'], 1)
        self.assertNotIn('case_name', saved['test'])
        self.assertEqual(saved['source_state'], 'working')
        accept_revision(self.vault, 'capacity', request['id'], 'Уточнено.', 'Заполнены исходные данные.')
        self.document.write_text('Рабочее правило изменилось.', encoding='utf-8')
        archive = dict(working, source='archive', revision=2, operation_id=str(uuid4()),
                       case_name='kit.calendar.CalendarTests.test_busy')
        second = apply_action(self.vault, 'plan', archive)
        self.assertEqual(second['test']['case_status'], 'skipped')
        self.assertEqual(second['source_state'], 'accepted')
        self.assertEqual(second['rule'], saved['rule'])
        for name in ('working_path', 'archive_path'):
            self.assertTrue((self.vault.path / second[name]).is_file())
        self.assertEqual(len(read_collaboration(self.vault, 'plan')['traces']), 2)
        self.assertEqual(apply_action(self.vault, 'plan', working), saved)
        with self.assertRaisesRegex(ValueError, 'редакция'):
            apply_action(self.vault, 'plan', dict(working, operation_id=str(uuid4())))
        with self.assertRaisesRegex(ValueError, 'Цитата'):
            apply_action(self.vault, 'plan', dict(archive, rule='Придуманное условие', operation_id=str(uuid4())))
        with self.assertRaisesRegex(ValueError, 'исходом'):
            apply_action(self.vault, 'plan', dict(archive, case_name='kit.calendar.NotObserved.test_passed',
                                                   operation_id=str(uuid4())))

    def test_only_recorded_task_and_its_own_test_event_can_be_linked(self):
        first = self.make_run('first')
        other = self.make_run('second')
        self.document.write_text('INV-2. Срок зависит от календаря.', encoding='utf-8')
        cmd = self.trace('first', 'Срок зависит от календаря.')
        for fields in (dict(task_id='missing'), dict(test_event_id=other['event_id']), dict(case_name='Fake')):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                apply_action(self.vault, 'plan', dict(cmd, **fields))
        self.assertIsNone(apply_action(self.vault, 'plan', cmd)['test'])
        self.assertEqual(apply_action(self.vault, 'plan', dict(cmd, operation_id=str(uuid4()),
                                                              test_event_id=first['event_id']))['test']['scope'], 'full')
        self.vault.add('stage', 'other', 'Другой этап')
        other_doc = section_paths(self.vault, 'other')['requirements']
        other_doc.write_text('Срок зависит от календаря.', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'другом этапе'):
            apply_action(self.vault, 'other', dict(cmd, operation_id=str(uuid4()), feature=''))


if __name__ == '__main__':
    unittest.main()
