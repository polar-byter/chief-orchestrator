"""Навигация рабочего места, сохранность документов и связь с заявками."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit import project_workspace
from kit.project_dispatch import _prompt
from kit.project_docs import _parser, execute
from kit.project_revisions import accept_revision, compare_revision, pending_requests, request_revision
from kit.project_vault import ProjectVault
from kit.project_workspace import document_snapshot, ensure_workspace, section_paths


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'проект'
        self.root.mkdir()
        self.vault = ProjectVault.connect(self.root, self.base / 'база')
        self.vault.add('stage', 'delivery', 'Поставка')
        self.vault.add('feature', 'docs', 'Документы', 'delivery')

    def draft(self):
        with self.vault.workspace('docs').open('a', encoding='utf-8') as stream:
            stream.write('Открывать требования и BDD из карточки.')

    def test_card_overview_and_sections_link_to_the_same_feature(self):
        overview = ensure_workspace(self.vault, 'docs')
        documents = section_paths(self.vault, 'docs')
        self.assertEqual(len(documents), 5)
        for source in (self.vault.card('docs'), overview, *documents.values()):
            text = source.read_text(encoding='utf-8').replace(r'\|', '|')
            for target in re.findall(r'\[\[([^]|#]+)', text):
                self.assertTrue((self.vault.path / (target + '.md')).is_file(), (source, target))
        for section in project_workspace.SECTIONS:
            path = documents[section.key].relative_to(self.vault.path).with_suffix('').as_posix()
            self.assertIn(path, self.vault.card('docs').read_text(encoding='utf-8'))
            self.assertIn(path, overview.read_text(encoding='utf-8'))
        self.assertIn('Нет данных', documents['effort'].read_text(encoding='utf-8'))

    def test_reopen_and_parallel_upgrade_preserve_notes_and_draft(self):
        card = self.vault.card('docs')
        # Имитируем карточку ранней версии, которая ещё не имеет навигации.
        card.write_text('Моё описание и решение РП.\n', encoding='utf-8')
        self.draft()
        draft = self.vault.workspace('docs').read_bytes()
        paths = list(section_paths(self.vault, 'docs').values())
        overview = ensure_workspace(self.vault, 'docs')
        for path in (*paths, overview):
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\nРучное уточнение.\n')
        before = {path: path.read_bytes() for path in (*paths, overview)}
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _: ensure_workspace(self.vault, 'docs'), range(3)))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(draft, self.vault.workspace('docs').read_bytes())
        self.assertTrue(card.read_text(encoding='utf-8').startswith('Моё описание и решение РП.'))
        self.assertEqual(card.read_text(encoding='utf-8').count('[[Документы/docs/Рабочее место|'), 1)

    def test_interrupted_creation_resumes_without_replacing_created_document(self):
        original = project_workspace.publish_new
        def interrupted(path, data):
            if path.name == 'BDD.md' and path.parent.name == 'retry':
                raise OSError('Прервано создание')
            return original(path, data)
        with patch.object(project_workspace, 'publish_new', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'Прервано'):
                self.vault.add('feature', 'retry', 'Повтор', 'delivery')
        requirements = section_paths(self.vault, 'retry')['requirements']
        requirements.write_text('Уточнение во время восстановления', encoding='utf-8')
        ensure_workspace(self.vault, 'retry')
        self.assertEqual(requirements.read_text(encoding='utf-8'), 'Уточнение во время восстановления')
        self.assertEqual(len(document_snapshot(self.vault, 'retry')), 5)

    def test_deleted_document_is_not_silently_recreated_from_template(self):
        target = section_paths(self.vault, 'docs')['nfr']
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'Восстановите'):
            ensure_workspace(self.vault, 'docs')
        self.assertFalse(target.exists())

    def test_same_named_features_and_other_vault_remain_separate(self):
        self.vault.add('feature', 'other', 'Документы', 'delivery')
        other_root = self.base / 'другой проект'
        other_root.mkdir()
        other = ProjectVault.connect(other_root)
        other.add('stage', 'docs', 'Документы')
        first = section_paths(self.vault, 'docs')['risks']
        first.write_text('Риск только одной фичи', encoding='utf-8')
        for vault, key in ((self.vault, 'other'), (other, 'docs')):
            self.assertNotIn('Риск только одной фичи', section_paths(vault, key)['risks'].read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):
            section_paths(self.vault, '../elsewhere')

    def test_open_section_uses_bound_vault_and_preserves_legacy_work_flag(self):
        for section in ('workspace', 'requirements', 'bdd', 'nfr', 'effort', 'risks'):
            args = _parser().parse_args(['--root', str(self.root), 'open', '--key', 'docs', '--section', section])
            with patch('kit.obsidian_app.open_note') as opened, patch('kit.project_docs.ensure_inbox_watcher'):
                result = execute(args)
            expected = ensure_workspace(self.vault, 'docs') if section == 'workspace' else section_paths(self.vault, 'docs')[section]
            opened.assert_called_once_with(self.vault.path, expected)
            self.assertEqual(result['note'], str(expected))
        args = _parser().parse_args(['--root', str(self.root), 'open', '--work', '--key', 'docs'])
        with patch('kit.obsidian_app.open_note') as opened, patch('kit.project_docs.ensure_inbox_watcher'):
            execute(args)
        opened.assert_called_once_with(self.vault.path, self.vault.workspace('docs'))
        args = _parser().parse_args(['--root', str(self.root), 'open', '--section', 'bdd'])
        with self.assertRaisesRegex(ValueError, '--key'):
            execute(args)

    def test_changed_document_supersedes_request_and_blocks_stale_acceptance(self):
        self.draft()
        request = request_revision(self.vault, 'docs')
        self.assertEqual(request_revision(self.vault, 'docs'), request)
        self.assertEqual(len(request['documents']), 5)
        target = section_paths(self.vault, 'docs')['nfr']
        with target.open('a', encoding='utf-8') as stream:
            stream.write('\nНоль потерянных правок.\n')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            accept_revision(self.vault, 'docs', request['id'], 'Готово', 'Ручная проверка')
        differences = compare_revision(self.vault, request['id'])
        self.assertIn('NFR.md', differences)
        self.assertIn('+Ноль потерянных правок.', differences)
        replacement = request_revision(self.vault, 'docs')
        self.assertNotEqual(request['id'], replacement['id'])
        self.assertEqual([replacement], pending_requests(self.vault))
        self.assertNotIn('Ноль потерянных правок.', _prompt(self.vault, request, 'session'))
        self.assertIn('Ноль потерянных правок.', _prompt(self.vault, replacement, 'session'))

    def test_archive_keeps_submitted_documents_after_later_edits(self):
        self.draft()
        request = request_revision(self.vault, 'docs')
        accepted = accept_revision(self.vault, 'docs', request['id'], 'Готово', 'Ручная проверка')
        note = self.vault.path / accepted['note']
        before = note.read_bytes()
        self.assertIn('Документы/docs/BDD.md', before.decode('utf-8'))
        target = section_paths(self.vault, 'docs')['bdd']
        target.write_text('Новые условия следующей редакции', encoding='utf-8')
        self.assertEqual(note.read_bytes(), before)
        self.assertNotIn('Новые условия', before.decode('utf-8'))
        self.assertEqual(accept_revision(self.vault, 'docs', request['id'], 'Готово', 'Ручная проверка'), accepted)

    def test_legacy_request_and_archive_without_documents_still_read_and_accept(self):
        self.draft()
        request = request_revision(self.vault, 'docs')
        # До обновления заявки не содержали снимков пяти документов.
        with self.vault.locked() as db:
            row = db.execute('SELECT document FROM revision_requests WHERE id=?', (request['id'],)).fetchone()
            old = json.loads(row[0])
            old.pop('documents')
            card = self.vault.card('docs')
            block = project_workspace.card_navigation('docs').encode('utf-8')
            source = card.read_bytes().replace(block, b'', 1)
            card.write_bytes(source + block)
            old['card_text'] = source.decode('utf-8')
            old['card_sha256'] = hashlib.sha256(source).hexdigest()
            db.execute('UPDATE revision_requests SET document=? WHERE id=?', (json.dumps(old), request['id']))
        self.assertIn('старой заявке', compare_revision(self.vault, request['id']))
        result = accept_revision(self.vault, 'docs', request['id'], 'Готово', 'Старая проверка')
        self.assertNotIn('documents', result)
        self.assertTrue((self.vault.path / result['note']).is_file())

    def test_compare_reports_deleted_document_without_changing_request(self):
        self.draft()
        request = request_revision(self.vault, 'docs')
        section_paths(self.vault, 'docs')['bdd'].unlink()
        self.assertIn('Документ удалён: Документы/docs/BDD.md', compare_revision(self.vault, request['id']))
        self.assertEqual(pending_requests(self.vault), [request])
