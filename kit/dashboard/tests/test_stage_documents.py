"""Рабочий Markdown, снимки редакций и квитанции браузерного сохранения."""
from codecs import BOM_UTF8
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from kit.dashboard import stage_documents as editor
from kit.project_revisions import accept_revision, request_revision
from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths


class StageDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        root = folder / 'Чиф'
        root.mkdir()
        self.vault = ProjectVault.connect(root, folder / 'база + примеры')
        self.vault.add('stage', 'docs', 'Документация')
        self.vault.add('feature', 'rules', 'Правила', 'docs')
        self.path = section_paths(self.vault, 'rules')['requirements']

    def read(self, revision=None):
        return editor.read_document(self.vault, 'docs', 'rules', 'requirements', revision)

    def command(self, text, **changes):
        before = self.read()
        return dict(project_id=self.vault.project_id, stage='docs', feature='rules', section='requirements',
                    expected_hash=before['hash'], expected_revision=before['revision'], operation_id=str(uuid4()),
                    author='Аналитик', text=text) | changes

    def save(self, data):
        return editor.save_document(self.vault, 'docs', data)

    def test_working_and_archive_belong_to_the_selected_note(self):
        archived = self.read('1')
        new = '## Новое правило\nПроверяем возврат в ту же фичу.\n'
        cmd = self.command(new)
        saved = self.save(cmd)
        self.assertEqual(self.save(cmd), saved)
        current = self.read()
        self.assertEqual(current['last_receipt'], saved)
        self.assertEqual(current['text'], new)
        self.assertEqual(archived, self.read(1))
        self.assertEqual((current['readonly'], archived['readonly']), (False, True))
        for obj, path in [(current, self.path), (archived, self.vault.path / 'История/rules/Редакция 0001.md')]:
            self.assertEqual(urlsplit(obj['obsidian_url']).scheme, 'obsidian')
            self.assertEqual(unquote(urlsplit(obj['obsidian_url']).query.removeprefix('path=')), str(path))
        self.assertEqual(saved['revision'], 1)
        self.path.write_text('Правка после получения ответа', encoding='utf-8')
        self.assertEqual(self.save(cmd), saved)
        self.assertEqual(self.read()['text'], 'Правка после получения ответа')
        with self.assertRaises(editor.DocumentConflict):
            self.save(cmd | {'text':'Другая операция с прежним ID'})

    def test_external_edit_and_new_accepted_revision_cannot_be_overwritten(self):
        cmd = self.command('Мой черновик')
        self.path.write_text('Изменение в Obsidian', encoding='utf-8')
        with self.assertRaises(editor.DocumentConflict):
            self.save(cmd)
        self.assertEqual(self.read()['text'], 'Изменение в Obsidian')
        cmd = self.command('Мой сверенный черновик')
        with self.vault.workspace('rules').open('a', encoding='utf-8') as note:
            note.write('Уточнить требования к документу.\n')
        request = request_revision(self.vault, 'rules')
        accept_revision(self.vault, 'rules', request['id'], 'Проверенный текст', 'Сверена формулировка')
        self.assertEqual(self.read()['revision'], 2)
        with self.assertRaises(editor.DocumentConflict):
            self.save(cmd)
        self.assertEqual(self.read(2)['hash'], cmd['expected_hash'])

    def test_concurrent_edits_preserve_one_winner_and_do_not_modify_any_other_file(self):
        sources = section_paths(self.vault, 'rules')
        other = {p:p.read_bytes() for p in sources.values() if p != self.path}
        commands = [self.command('Версия один'), self.command('Версия два')]
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = [workers.submit(self.save, cmd) for cmd in commands]
        success = [result.result() for result in results if not result.exception()]
        self.assertEqual(len(success), 1)
        self.assertTrue(any(isinstance(result.exception(), editor.DocumentConflict) for result in results))
        self.assertEqual(success[0]['hash'], self.read()['hash'])
        self.assertEqual(other, {p:p.read_bytes() for p in other})

    def test_recovery_after_atomic_replace_preserves_the_receipt_and_later_external_edit(self):
        original_write = editor._replace_checked
        def fail_after_file(*args):
            original_write(*args)
            raise OSError('Прервано перед записью квитанции')
        cmd = self.command('Новый текст')
        with patch.object(editor, '_replace_checked', side_effect=fail_after_file), self.assertRaises(OSError):
            self.save(cmd)
        recovered = self.save(cmd)
        self.assertEqual(recovered, self.read()['last_receipt'])
        later = self.command('Следующий текст')
        with patch.object(editor, '_replace_checked', side_effect=fail_after_file), self.assertRaises(OSError):
            self.save(later)
        self.path.write_text('Внешняя правка после сбоя', encoding='utf-8')
        with self.assertRaises(editor.DocumentConflict):
            self.save(later)
        self.assertEqual(self.read()['text'], 'Внешняя правка после сбоя')

    def test_browser_newlines_preserve_existing_crlf_and_utf8_bom(self):
        self.path.write_bytes(BOM_UTF8 + b'# Before\r\n')
        self.save(self.command('# After\n\nNew.\n'))
        self.assertEqual(self.path.read_bytes(), BOM_UTF8 + b'# After\r\n\r\nNew.\r\n')

    def test_boundaries_legacy_archive_and_read_only_reads(self):
        self.vault.add('stage', 'other', 'Другой этап')
        self.vault.add('feature', 'separate', 'Соседний документ', 'other')
        original = self.read()
        with self.vault.reading() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='stage_document_edits'").fetchone())
        for other in [dict(feature='separate'), dict(stage='other'), dict(section='card'), dict(section='../file'),
                      dict(project_id=str(uuid4())), dict(revision=1), dict(expected_revision=True), dict(author=' ')]:
            with self.subTest(other=other), self.assertRaises(ValueError):
                self.save(self.command('Чужой текст', **other))
        self.assertEqual(self.read(), original)
        with self.vault.locked() as db:
            db.execute("UPDATE revision_history SET document=json_remove(document,'$.documents') WHERE record_key='rules'")
        with self.assertRaises(FileNotFoundError):
            self.read(1)
        self.assertEqual(self.read()['text'], original['text'])
