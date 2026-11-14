"""Доработка этапа и фичи через ту же базу с сохранением исходных заметок."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit.journal import TaskJournal
from kit.project_revisions import accept_revision, compare_revision, ensure_history, marked_for_submission, pending_requests, request_revision
from kit.project_vault import ProjectVault


class ProjectRevisionTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.project = Path(area.name) / 'project'
        self.project.mkdir()
        self.vault = ProjectVault.connect(self.project, Path(area.name) / 'notes')
        self.vault.add('stage', 'plan', 'План')
        self.vault.add('feature', 'export', 'Выгрузка таблицы', 'plan', 'Сохранить строки CSV.')

    def draft(self, key, text):
        path = self.vault.workspace(key)
        old = path.read_text(encoding='utf-8').partition('## Что изменить\n')[0]
        path.write_text(old + '## Что изменить\n\n' + text, encoding='utf-8')

    def test_project_identity_parent_and_repeated_submission(self):
        self.draft('export', 'Добавить разделитель «;».')
        original = request_revision(self.vault, 'export')
        reopened = ProjectVault.load(self.project)
        self.assertEqual(original, request_revision(reopened, 'export'))
        self.assertEqual([original], pending_requests(reopened))
        for key, value in dict(project_id=reopened.project_id, stage='plan', kind='feature',
                               key='export', base_revision=1, text='Добавить разделитель «;».').items():
            self.assertEqual(original[key], value)
        self.assertIn('Сохранить строки CSV.', original['card_text'])
        self.assertTrue((reopened.path / original['base_note']).is_file())
        self.assertEqual(original['context'], [dict(key='plan', title='План', revision=1)])

    def test_empty_note_and_missing_section_do_not_submit(self):
        with self.assertRaisesRegex(ValueError, 'Опишите'):
            request_revision(self.vault, 'plan')
        self.vault.workspace('plan').write_text('Мои заметки', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'заголовок'):
            request_revision(self.vault, 'plan')
        self.assertEqual(pending_requests(self.vault), [])

    def test_checkbox_delivers_draft_without_invalidating_approval(self):
        self.draft('plan', 'Пример флажка в требованиях:\n- [x] Передать Чифу')
        path = self.vault.workspace('plan')
        draft = path.read_text(encoding='utf-8')
        source = self.vault.card('plan').relative_to(self.vault.path).with_suffix('').as_posix()
        self.assertIn(f'[[{source}|Открыть карточку]]', draft)
        launcher = r'.\kit\python.cmd' if os.name == 'nt' else 'sh kit/python.sh'
        self.assertIn(f'{launcher} -m kit.project_docs submit --key plan', draft)
        self.assertFalse(marked_for_submission(draft))
        request = request_revision(self.vault, 'plan')
        clicked = draft.replace('- [ ] Передать Чифу', '- [X] Передать Чифу', 1)
        path.write_text(clicked, encoding='utf-8')
        self.assertTrue(marked_for_submission(clicked))
        with self.assertRaisesRegex(ValueError, 'после отметки'):
            request_revision(self.vault, 'plan', expected_workspace=draft.encode('utf-8'))
        self.assertEqual(request, request_revision(self.vault, 'plan'))
        accept_revision(self.vault, 'plan', request['id'], 'Готово', 'Сверен календарь.')

    def test_manual_acceptance_and_next_stage_revision(self):
        self.draft('plan', 'Перенести срок на вторник.')
        request = request_revision(self.vault, 'plan')
        self.assertEqual(request['stage'], 'plan')
        self.assertEqual(request['kind'], 'stage')
        card = self.vault.card('plan')
        card_before = card.read_bytes()
        self.vault.add('feature', 'report', 'Отчёт', 'plan')
        accepted = accept_revision(self.vault, 'plan', request['id'], 'Срок перенесён.', 'РП проверил календарь.')
        self.assertEqual([item['key'] for item in accepted['context']], ['export'])
        self.assertEqual([item['key'] for item in accepted['accepted_context']], ['export', 'report'])
        self.assertEqual(accepted['revision'], 2)
        self.assertEqual(request_revision(self.vault, 'plan'), accepted)
        self.assertEqual(accept_revision(self.vault, 'plan', request['id'], accepted['result'], accepted['evidence']), accepted)
        with self.assertRaisesRegex(ValueError, 'другое решение'):
            accept_revision(self.vault, 'plan', request['id'], 'Совершенно иное решение', accepted['evidence'])
        self.assertEqual(card_before, card.read_bytes())
        self.assertEqual([], pending_requests(self.vault))
        self.assertIn('История/plan/Редакции', card.read_text(encoding='utf-8'))
        note = (self.vault.path / accepted['note']).read_text(encoding='utf-8')
        self.assertIn('История/plan/Редакция 0001', note)
        self.assertIn('РП проверил календарь.', note)
        self.draft('plan', 'Добавить этап проектирования.')
        following = request_revision(self.vault, 'plan')
        self.assertEqual((following['base_revision'], following['base_result']), (2, 'Срок перенесён.'))
        self.assertNotEqual(following['task_id'], request['task_id'])

    def test_edits_to_notes_block_acceptance_and_allow_resubmission(self):
        self.draft('export', 'Сохранять суммы с копейками.')
        for path in (self.vault.card('export'), self.vault.workspace('export')):
            with self.subTest(note=path.name):
                request = request_revision(self.vault, 'export')
                with path.open('a', encoding='utf-8') as stream:
                    stream.write('\nПравка аналитика.\n')
                with self.assertRaisesRegex(ValueError, 'изменились'):
                    accept_revision(self.vault, 'export', request['id'], 'Готово', 'Сверены суммы')
                replacement = request_revision(self.vault, 'export')
                with self.assertRaisesRegex(ValueError, 'сменилась'):
                    accept_revision(self.vault, 'export', request['id'], 'Готово', 'Сверены суммы')
                self.assertEqual([replacement], pending_requests(self.vault))
                self.assertIn('Правка аналитика.', path.read_text(encoding='utf-8'))

    def test_other_project_or_card_cannot_accept_request(self):
        self.draft('export', 'Открывать таблицу после выгрузки.')
        request = request_revision(self.vault, 'export')
        other = self.project.parent / 'another'
        other.mkdir()
        other_vault = ProjectVault.connect(other, self.project.parent / 'another-vault')
        other_vault.add('stage', 'export', 'Выгрузка')
        for vault, key in ((self.vault, 'plan'), (other_vault, 'export')):
            with self.assertRaisesRegex(ValueError, 'не принадлежит'):
                accept_revision(vault, key, request['id'], 'Готово', 'Проверено')

    def test_compare_shows_card_and_working_text_against_saved_request(self):
        self.draft('export', 'Округлять до рублей.')
        request = request_revision(self.vault, 'export')
        self.assertIn('совпадают', compare_revision(self.vault, request['id']))
        card = self.vault.card('export')
        card.write_text(card.read_text(encoding='utf-8').replace('строки CSV.', 'строки JSON.'), encoding='utf-8')
        self.draft('export', 'Сохранять копейки.')
        changes = compare_revision(self.vault, request['id'])
        self.assertIn('--- ' + card.relative_to(self.vault.path).as_posix(), changes)
        work = self.vault.workspace('export')
        self.assertIn('+++ ' + work.relative_to(self.vault.path).as_posix(), changes)
        self.assertIn('-Сохранить строки CSV.', changes)
        self.assertIn('+Сохранить строки JSON.', changes)
        self.assertIn('-Округлять до рублей.', changes)
        self.assertIn('+Сохранять копейки.', changes)
        self.assertEqual([request], pending_requests(self.vault))

    def test_acceptance_requires_the_matching_finished_task_and_green_full_test(self):
        self.draft('export', 'Группировать по месяцу.')
        request = request_revision(self.vault, 'export')
        args = (self.vault, 'export', request['id'], 'Формируются листы по месяцам.', 'Проверено деление года.')
        with self.assertRaisesRegex(ValueError, 'этой заявки'):
            accept_revision(*args, task='other')
        task = request['task_id']
        journal = TaskJournal(self.project)
        folder = journal.task_dir(task)
        folder.mkdir(parents=True)
        journal.append(task, 'start', title='Выгрузка по месяцам')
        with self.assertRaisesRegex(ValueError, 'Завершите'):
            accept_revision(*args, task=task)
        log = 'test-' + 'e' * 32 + '.log'
        journal.append(task, 'test', status='passed', scope='full', returncode=0, log=log)
        journal.append(task, 'finish')
        with self.assertRaisesRegex(ValueError, 'журнала полного'):
            accept_revision(*args, task=task)
        (folder / log).write_text('Ran 2 tests\nOK', encoding='utf-8')
        accepted = accept_revision(*args, task=task)
        note = (self.vault.path / accepted['note']).read_text(encoding='utf-8')
        self.assertIn((folder / log).as_uri(), note)
        self.assertIn((folder / 'events.jsonl').as_uri(), note)

    def test_failed_or_skipped_final_result_cannot_confirm_task(self):
        self.draft('export', 'Добавить графу Итого.')
        request = request_revision(self.vault, 'export')
        task = request['task_id']
        journal = TaskJournal(self.project)
        journal.task_dir(task).mkdir(parents=True)
        journal.append(task, 'start')
        journal.append(task, 'test', status='passed', scope='full', phase='baseline', returncode=0)
        journal.append(task, 'test', status='skipped', scope='full', returncode=0)
        journal.append(task, 'test', status='passed', scope='worker', returncode=0)
        journal.append(task, 'finish')
        with self.assertRaisesRegex(ValueError, 'полный тест'):
            accept_revision(self.vault, 'export', task, 'Итого рассчитано', 'Проверка формулы', task=task)
        self.assertEqual([request], pending_requests(self.vault))

    def test_retries_and_interrupted_archive_publish_only_one_decision(self):
        self.draft('export', 'Выгружать нулевые строки.')
        with ThreadPoolExecutor(3) as pool:
            ids = list(pool.map(lambda _: request_revision(self.vault, 'export')['id'], range(3)))
        self.assertEqual(len(set(ids)), 1)
        args = self.vault, 'export', ids[0], 'Нулевые строки видны', 'Сверен пример с одним нулём.'
        from kit import project_revisions
        publish = project_revisions._archive
        def fail_after_publish(path, text):
            publish(path, text)
            raise OSError('Диск отключён')
        with patch.object(project_revisions, '_archive', side_effect=fail_after_publish):
            with self.assertRaisesRegex(OSError, 'отключён'):
                accept_revision(*args)
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: accept_revision(*args), range(3)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(list((self.vault.path / 'История/export').glob('Редакция *.md'))), 2)
        self.assertTrue(ensure_history(self.vault, 'export').is_file())
